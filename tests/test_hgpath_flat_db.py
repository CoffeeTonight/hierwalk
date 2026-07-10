"""hgpath flat DB tests."""

from __future__ import annotations

from pathlib import Path

from hgpath.flat_db import FLAT_JSON_NAME, load_or_build_flat_db


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_hgpath_flat_db_build_and_hit(tmp_path: Path):
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
    work = tmp_path / "work"
    logs: list[str] = []

    db1, _s1 = load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        on_log=logs.append,
    )
    assert (work / FLAT_JSON_NAME).is_file()
    assert db1.module_count >= 2
    assert any("flat-db built" in line for line in logs)

    logs.clear()
    db2, _s2 = load_or_build_flat_db(
        [top_v],
        top="top",
        work_dir=work,
        on_log=logs.append,
    )
    assert any("flat-db hit" in line for line in logs)
    assert db2.module_index == db1.module_index