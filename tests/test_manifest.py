"""Source manifest: content-hash only (no mtime/size change detection)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.manifest import (
    build_source_manifest,
    collect_index_digest_paths,
    config_cache_key,
    hash_paths_parallel,
    manifest_diff,
    manifest_is_current,
)


def _write_design(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  mid u_mid ( );
endmodule
module mid;
  leaf u_leaf ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")
    return fl, rtl


def test_manifest_ignores_mtime_only_change(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    before = build_source_manifest(fl)

    time.sleep(0.02)
    os.utime(rtl, None)
    after = build_source_manifest(parse_filelist(fl_path))

    assert manifest_is_current(before, after)


def test_manifest_detects_same_size_content_change(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    before = build_source_manifest(fl)

    text = rtl.read_text(encoding="utf-8")
    rtl.write_text(text.replace("u_mid", "u_mtx"), encoding="utf-8")
    after = build_source_manifest(parse_filelist(fl_path))

    key = str(rtl.resolve())
    assert before[key] != after[key]
    changed, removed, added = manifest_diff(before, after)
    assert key in changed
    assert not removed
    assert not added


def test_build_source_manifest_parallel_matches_serial(tmp_path):
    fl_path, _rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    paths = collect_index_digest_paths(fl_path, fl)
    serial = hash_paths_parallel(paths, jobs=1)
    parallel = hash_paths_parallel(paths, jobs=4)
    assert serial == parallel
    assert build_source_manifest(fl, path_digests=serial) == build_source_manifest(
        fl,
        path_digests=parallel,
    )


def test_single_hash_pass_for_index_cache(tmp_path, monkeypatch):
    fl_path, _rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    paths = collect_index_digest_paths(fl_path, fl)
    reads: list[str] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        key = str(self.resolve())
        if key in paths:
            reads.append(key)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    digests = hash_paths_parallel(paths, jobs=1)
    config_cache_key(
        fl_path,
        fl,
        cache_version=1,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
        ignore_filelists=[],
        path_digests=digests,
    )
    build_source_manifest(fl, path_digests=digests)
    assert len(reads) == len(paths)