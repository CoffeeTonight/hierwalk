"""pyslangwalk: module-index scoped hierarchy resolve."""

from __future__ import annotations

from pathlib import Path

import pytest

pyslang = pytest.importorskip("pyslang")

from hierwalk.hierarchy_grep import HierarchyGrepSession
from hierwalk.pyslang_walk import PyslangWalkSession


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_pyslang_walk_resolves_inst_and_port(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
          child u_b ();
        endmodule
        """,
    )
    grep = HierarchyGrepSession.from_rtl_paths(
        [top_v],
        build_file_index_background=False,
    )
    session = PyslangWalkSession.from_grep_session(grep)
    res = session.resolve("top.u_a.out", top="top")
    assert res.ok, res.error
    assert any(n.segment == "u_a" for n in res.nodes)
    assert res.nodes[-1].segment == "out"
    assert res.scoped_files
    assert all(Path(f).name == "top.v" for f in res.scoped_files)


def test_pyslang_walk_wire_inst_collision(tmp_path: Path):
    """``wire u_b`` must not hide ``zz_scope_B u_b`` (pyslang sees HierarchyInstantiation)."""
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module zz_scope_B (output logic scope_probe);
          assign scope_probe = 1'b1;
        endmodule
        module top;
          wire u_b;
          zz_scope_B u_b (.scope_probe());
        endmodule
        """,
    )
    grep = HierarchyGrepSession.from_rtl_paths(
        [top_v],
        build_file_index_background=False,
    )
    session = PyslangWalkSession.from_grep_session(grep)
    res = session.resolve("top.u_b.scope_probe", top="top")
    assert res.ok, res.error
    assert any(n.segment == "u_b" and n.child_module == "zz_scope_B" for n in res.nodes)


def test_pyslang_walk_prefers_non_decoy_module_file(tmp_path: Path):
    """Multi-definition: try all files; skip decoy that lacks the instance."""
    decoy = _write(
        tmp_path,
        "zz_scope_decoy.v",
        """
        module zz_scope_A;
          // empty decoy
        endmodule
        """,
    )
    real = _write(
        tmp_path,
        "zz_scope_a.v",
        """
        module zz_scope_B (output logic scope_probe);
          assign scope_probe = 1'b1;
        endmodule
        module zz_scope_A;
          wire u_b;
          zz_scope_B u_b (.scope_probe());
        endmodule
        """,
    )
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
          zz_scope_A u_a ();
        endmodule
        """,
    )
    # Put decoy first in index order (as zigzag does)
    session = PyslangWalkSession.from_module_index(
        {
            "top": [top_v],
            "zz_scope_A": [decoy, real],
            "zz_scope_B": [real],
        }
    )
    res = session.resolve("top.u_a.u_b.scope_probe", top="top")
    assert res.ok, res.error
    assert any("decoy" not in n.file for n in res.nodes if n.segment == "u_b")


def test_pyslang_walk_missing_inst(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
        endmodule
        """,
    )
    grep = HierarchyGrepSession.from_rtl_paths(
        [top_v],
        build_file_index_background=False,
    )
    session = PyslangWalkSession.from_grep_session(grep)
    res = session.resolve("top.u_missing.x", top="top")
    assert res.ok is False
    assert "not found" in res.error.lower() or "not an instance" in res.error.lower()
