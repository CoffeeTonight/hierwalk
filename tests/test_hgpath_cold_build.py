"""Cold flat-DB build: single dump, normalized paths, lazy file index."""

from __future__ import annotations

import json
from pathlib import Path

from hgpath.flat_db import FLAT_JSON_NAME, load_or_build_flat_db, resolve_flat_db_path
from hierwalk.hierarchy_grep import GREP_HIE_JSON_NAME, resolve_grep_hie_path


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_flat_db_single_dump_and_legacy_alias(tmp_path: Path):
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
    fl = tmp_path / "design.f"
    fl.write_text(f"{top_v}\n", encoding="utf-8")
    work = tmp_path / "work"

    db, session = load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        filelist=str(fl.resolve()),
        index_cwd=str(tmp_path),
    )
    flat_path = resolve_flat_db_path(work)
    legacy_path = resolve_grep_hie_path(work)
    assert flat_path.is_file()
    assert legacy_path.is_file() or legacy_path.is_symlink()
    raw = json.loads(flat_path.read_text(encoding="utf-8"))
    assert raw.get("paths_normalized") is True
    assert raw.get("filelist") == str(fl.resolve())
    assert session.resolve("top.u_a", top="top")["ok"] is True
    assert db.module_count >= 2

    db2, _ = load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        filelist=str(fl.resolve()),
        index_cwd=str(tmp_path),
    )
    assert db2.module_index == db.module_index