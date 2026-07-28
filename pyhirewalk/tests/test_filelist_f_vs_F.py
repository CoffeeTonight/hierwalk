"""-f vs -F semantics and context_id stability."""

from __future__ import annotations

from pathlib import Path

from pyhirewalk.context import build_context, context_id_from_parts
from pyhirewalk.filelist import expand_filelist


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_dash_f_paths_relative_to_nested_filelist(tmp_path: Path) -> None:
    """-f: nested list location and its RTL paths are relative to the nested .f dir."""
    run = tmp_path / "run"
    run.mkdir()
    rtl_a = tmp_path / "ip_a" / "a.v"
    _write(rtl_a, "module a; endmodule\n")
    nested = tmp_path / "ip_a" / "ip.f"
    _write(nested, "a.v\n")
    top = run / "top.f"
    _write(top, f"-f {nested}\n+define+FOO=1\n")

    fl = expand_filelist(top, index_cwd=run)
    assert rtl_a.resolve() in [p.resolve() for p in fl.source_files]
    assert fl.defines.get("FOO") == "1"
    assert fl.filelist_edges
    kinds = {k for _, _, k in fl.filelist_edges}
    assert "-f" in kinds


def test_dash_F_paths_relative_to_index_cwd(tmp_path: Path) -> None:
    """
    -F: nested .f is found from index_cwd; paths *inside* nested.f
    are also relative to index_cwd (not the nested .f directory).
    """
    run = tmp_path / "run"
    run.mkdir()
    # RTL lives under run/, not next to lists/
    rtl = run / "core.v"
    _write(rtl, "module core; endmodule\n")
    lists = tmp_path / "lists"
    nested = lists / "core.f"
    # path relative to run (index_cwd), not lists/
    _write(nested, "core.v\n")
    top = run / "top.f"
    # -F finds nested relative to run → need path from run to lists/core.f
    _write(top, "-F ../lists/core.f\n")

    fl = expand_filelist(top, index_cwd=run)
    assert rtl.resolve() in [p.resolve() for p in fl.source_files]
    kinds = {k for _, _, k in fl.filelist_edges}
    assert "-F" in kinds


def test_dash_F_wrong_if_treated_as_dash_f(tmp_path: Path) -> None:
    """If -F were wrongly resolved like -f, core.v next to nested.f would be used."""
    run = tmp_path / "run"
    run.mkdir()
    lists = tmp_path / "lists"
    wrong = lists / "core.v"
    _write(wrong, "module wrong; endmodule\n")
    right = run / "core.v"
    _write(right, "module core; endmodule\n")
    nested = lists / "core.f"
    _write(nested, "core.v\n")
    top = run / "top.f"
    _write(top, "-F ../lists/core.f\n")

    fl = expand_filelist(top, index_cwd=run)
    srcs = {p.resolve() for p in fl.source_files}
    assert right.resolve() in srcs
    assert wrong.resolve() not in srcs


def test_incdir_define_and_comment_strip(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    rtl = run / "t.sv"
    _write(rtl, "module t; endmodule\n")
    inc = run / "include"
    inc.mkdir()
    top = run / "top.f"
    _write(
        top,
        """
        // comment
        +incdir+./include
        +define+SYNTHESIS
        +define+WIDTH=8
        t.sv  // trailing
        # hash comment
        """,
    )
    fl = expand_filelist(top, index_cwd=run)
    assert fl.defines["SYNTHESIS"] == "1"
    assert fl.defines["WIDTH"] == "8"
    assert any(p.resolve() == inc.resolve() for p in fl.incdirs)
    assert rtl.resolve() in [p.resolve() for p in fl.source_files]


def test_context_id_changes_with_defines(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    rtl = run / "t.v"
    _write(rtl, "module t; endmodule\n")
    top = run / "top.f"
    _write(top, "t.v\n+define+A=1\n")

    c1 = build_context(top, index_cwd=run)
    c2 = build_context(top, index_cwd=run, extra_defines={"A": "2"})
    c3 = build_context(top, index_cwd=run)
    assert c1.context_id == c3.context_id
    assert c1.context_id != c2.context_id


def test_provenance_via_filelist(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    ip = tmp_path / "ip"
    rtl = ip / "u.v"
    _write(rtl, "module u; endmodule\n")
    nested = ip / "u.f"
    _write(nested, "u.v\n")
    top = run / "top.f"
    _write(top, f"-f {nested}\n")

    fl = expand_filelist(top, index_cwd=run)
    assert fl.source_via_filelist[rtl.resolve()] == nested.resolve()
    assert "u.f" in fl.source_filelist_chain[rtl.resolve()]


def test_context_id_helper_stable() -> None:
    a = context_id_from_parts(
        top_filelist=Path("/x/top.f"),
        index_cwd=Path("/x"),
        sources=[Path("/x/a.v")],
        incdirs=[Path("/x")],
        defines={"FOO": "1"},
    )
    b = context_id_from_parts(
        top_filelist=Path("/x/top.f"),
        index_cwd=Path("/x"),
        sources=[Path("/x/a.v")],
        incdirs=[Path("/x")],
        defines={"FOO": "1"},
    )
    assert a == b
    assert len(a) == 16
