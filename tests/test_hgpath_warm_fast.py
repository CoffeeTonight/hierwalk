"""Warm-path cache fast load tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hgpath.flat_db import (
    FLAT_JSON_NAME,
    load_or_build_flat_db,
    try_load_flat_db_cache,
)
from hgpath.tree_db import TreeDb
from hierwalk.hierarchy_grep import (
    dump_grep_hie,
    grep_hie_filelist_match,
    load_grep_hie,
)


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def _tiny_filelist(tmp_path: Path, top_v: str) -> Path:
    fl = tmp_path / "design.f"
    fl.write_text(f"{top_v}\n", encoding="utf-8")
    return fl.resolve()


def test_grep_hie_paths_normalized_fast_load(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    filelist = _tiny_filelist(tmp_path, top_v)
    work = tmp_path / "work"
    db1, _ = load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        filelist=str(filelist),
        index_cwd=str(tmp_path),
    )
    raw = json.loads((work / FLAT_JSON_NAME).read_text(encoding="utf-8"))
    assert raw.get("paths_normalized") is True
    assert raw.get("filelist") == str(filelist)

    t0 = time.perf_counter()
    cached = load_grep_hie(work / FLAT_JSON_NAME)
    assert time.perf_counter() - t0 < 0.25
    assert cached["rtl_paths"] == db1.rtl_paths


def test_try_load_flat_db_cache_skips_fingerprint_miss(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top (); endmodule\n")
    filelist = _tiny_filelist(tmp_path, top_v)
    work = tmp_path / "work"
    load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        filelist=str(filelist),
        index_cwd=str(tmp_path),
    )
    assert try_load_flat_db_cache(
        work_dir=work,
        filelist=str(filelist),
        index_cwd=str(tmp_path),
    ) is not None
    filelist.write_text(f"{top_v}\n// touched\n", encoding="utf-8")
    assert not grep_hie_filelist_match(
        load_grep_hie(work / FLAT_JSON_NAME),
        filelist,
        index_cwd=str(tmp_path),
    )
    assert try_load_flat_db_cache(
        work_dir=work,
        filelist=str(filelist),
        index_cwd=str(tmp_path),
    ) is None


def test_tree_save_if_changed(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    tree = TreeDb(work_dir=work, path=work / "hgpath_tree.json")
    assert tree.save_if_changed() is False
    tree.insert_result(
        "top.u_a",
        {
            "ok": True,
            "ambiguous": False,
            "error": "",
            "nodes": [
                {
                    "segment": "top",
                    "role": "module",
                    "module": "top",
                    "file": str(tmp_path / "top.v"),
                },
                {
                    "segment": "u_a",
                    "role": "inst",
                    "module": "top",
                    "child_module": "child",
                    "child_decl_file": str(tmp_path / "top.v"),
                },
            ],
        },
    )
    assert tree.save_if_changed() is True
    assert tree.save_if_changed() is False