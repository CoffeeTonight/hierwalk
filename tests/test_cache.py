"""Disk cache for DesignIndex and elaboration."""

from __future__ import annotations

import time

from hierwalk.cache import (
    CACHE_VERSION,
    ScanInstCacheBundle,
    build_design_index,
    cache_path_for,
    config_cache_key,
    elab_cache_key,
    load_cache,
    load_or_build_index,
    save_cache,
    store_cached_elab,
)
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.manifest import build_source_manifest
from hierwalk.preprocess import preprocess_file


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


def test_save_cache_preserves_live_module_bodies(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    index = build_design_index(
        fl,
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
        ignore_filelists=[],
        jobs=1,
    )
    body = index.module_body("mid")
    assert body
    index._instance_cache[("mid", "default")] = list(index.modules["mid"].instances)

    cache_dir = tmp_path / "cache"
    cfg = config_cache_key(
        fl_path,
        fl,
        cache_version=CACHE_VERSION,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
        ignore_filelists=[],
    )
    bundle = ScanInstCacheBundle(
        version=CACHE_VERSION,
        config_key=cfg,
        source_manifest=build_source_manifest(fl),
        index=index,
    )
    save_cache(cache_path_for(cache_dir, cfg), bundle)

    assert index.modules["mid"].body == body
    assert ("mid", "default") in index._instance_cache


def test_cache_roundtrip_pickle(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    index = build_design_index(fl, ignore_paths=[], ignore_path_files=[], ignore_modules=[],
            ignore_filelists=[], jobs=1)
    root, rows = elaborate(index, "top")

    cache_dir = tmp_path / "cache"
    cfg = config_cache_key(
        fl_path,
        fl,
        cache_version=CACHE_VERSION,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
    )
    bundle = ScanInstCacheBundle(
        version=CACHE_VERSION,
        config_key=cfg,
        source_manifest=build_source_manifest(fl),
        index=index,
        elab={elab_cache_key("top", None): (root, rows)},
    )
    path = cache_path_for(cache_dir, cfg)
    save_cache(path, bundle)
    store_cached_elab(
        bundle,
        "top",
        None,
        root,
        rows,
        cache_dir=cache_dir,
        use_cache=True,
    )

    loaded = load_cache(path, cache_dir=cache_dir)
    assert loaded is not None
    assert loaded.config_key == cfg
    assert set(loaded.index.modules) == {"top", "mid", "leaf"}
    assert loaded.index.modules["mid"].body == ""
    assert elab_cache_key("top", None) in loaded.elab
    cached_root, cached_rows = loaded.elab[elab_cache_key("top", None)]
    assert cached_root.module == "top"
    assert len(cached_rows) == 3


def test_load_or_build_index_cache_hit(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    cache_dir = tmp_path / "cache"

    index1, bundle1, hit1, rebuilt1, _inc1, _path1 = load_or_build_index(
        fl_path,
        fl,
        cache_dir=cache_dir,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )
    assert hit1 is False
    assert rebuilt1 is True
    assert len(index1.modules) == 3

    index2, bundle2, hit2, rebuilt2, _inc2, _path2 = load_or_build_index(
        fl_path,
        fl,
        cache_dir=cache_dir,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )
    assert hit2 is True
    assert rebuilt2 is False
    assert set(index2.modules) == set(index1.modules)


def test_cache_incremental_on_rtl_change(tmp_path):
    fl_path, rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    cache_dir = tmp_path / "cache"

    load_or_build_index(
        fl_path,
        fl,
        cache_dir=cache_dir,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )

    time.sleep(0.01)
    rtl.write_text(
        rtl.read_text(encoding="utf-8") + "\nmodule extra; endmodule\n",
        encoding="utf-8",
    )
    fl2 = parse_filelist(fl_path)
    index2, _bundle2, hit2, rebuilt2, inc2, _path2 = load_or_build_index(
        fl_path,
        fl2,
        cache_dir=cache_dir,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )
    assert hit2 is False
    assert rebuilt2 is True
    assert inc2 is True
    assert "extra" in index2.modules


def test_store_cached_elab_persists(tmp_path):
    fl_path, _rtl = _write_design(tmp_path)
    fl = parse_filelist(fl_path)
    cache_dir = tmp_path / "cache"
    index, bundle, _hit, _rebuilt, _inc, _path = load_or_build_index(
        fl_path,
        fl,
        cache_dir=cache_dir,
        extra_defines={},
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
            ignore_filelists=[],
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )
    root, rows = elaborate(index, "top")
    store_cached_elab(
        bundle,
        "top",
        None,
        root,
        rows,
        cache_dir=cache_dir,
        use_cache=True,
    )

    path = cache_path_for(cache_dir, bundle.config_key)
    loaded = load_cache(path, cache_dir=cache_dir)
    assert loaded is not None
    assert elab_cache_key("top", None) in loaded.elab