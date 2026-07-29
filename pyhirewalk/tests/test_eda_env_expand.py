"""EDA-style $VAR / ${VAR} expansion in filelists."""

from __future__ import annotations

from pathlib import Path

from pyhirewalk.filelist.envexpand import expand_eda_env, find_env_refs
from pyhirewalk.filelist.expand import expand_filelist


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_identifier_boundary_proj_vs_project() -> None:
    # Naive str.replace("$PROJ", ...) would corrupt $PROJECT
    out, missing = expand_eda_env(
        "$PROJECT/rtl/$PROJ/ip",
        {"PROJ": "/short", "PROJECT": "/long"},
    )
    assert out == "/long/rtl//short/ip"
    assert missing == []


def test_braces_glued_suffix() -> None:
    out, missing = expand_eda_env("${PROJ}_build/a.v", {"PROJ": "/work"})
    assert out == "/work_build/a.v"
    assert not missing


def test_unset_reported() -> None:
    out, missing = expand_eda_env("$MISSING/a.v", {})
    assert out == "$MISSING/a.v"
    assert missing == ["MISSING"]


def test_find_refs() -> None:
    assert find_env_refs("-f $A/${B}/c.f") == ["A", "B"]


def test_filelist_source_and_incdir_and_nested_f(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    rtl = proj / "rtl"
    ip = proj / "ip" / "uart"
    _w(rtl / "top.sv", "module top; endmodule\n")
    _w(rtl / "include" / "defs.vh", "//\n")
    _w(ip / "u.sv", "module u; endmodule\n")
    _w(ip / "files.f", "u.sv\n")

    top_f = tmp_path / "top.f"
    _w(
        top_f,
        """
        +incdir+$RTL_ROOT/include
        -f $PROJ/ip/uart/files.f
        $RTL_ROOT/top.sv
        """,
    )

    env = {"PROJ": str(proj), "RTL_ROOT": str(rtl)}
    fl = expand_filelist(top_f, env=env, index_cwd=tmp_path)

    srcs = {p.resolve() for p in fl.source_files}
    assert (rtl / "top.sv").resolve() in srcs
    assert (ip / "u.sv").resolve() in srcs
    assert any(p.resolve() == (rtl / "include").resolve() for p in fl.incdirs)
    assert not fl.unresolved_env


def test_dash_F_env_then_index_cwd(tmp_path: Path) -> None:
    """-F: env-expand nested path; content relative to index_cwd."""
    run = tmp_path / "run"
    run.mkdir()
    lists = tmp_path / "lists"
    _w(run / "core.v", "module core; endmodule\n")
    _w(lists / "core.f", "core.v\n")  # relative to run (= index_cwd), not lists/
    top = run / "top.f"
    _w(top, "-F $LISTS/core.f\n")

    fl = expand_filelist(
        top,
        env={"LISTS": str(lists)},
        index_cwd=run,
    )
    assert (run / "core.v").resolve() in {p.resolve() for p in fl.source_files}


def test_unset_env_in_filelist_errors(tmp_path: Path) -> None:
    top = tmp_path / "top.f"
    _w(top, "$NO_SUCH_ROOT/a.v\n")
    fl = expand_filelist(top, env={}, index_cwd=tmp_path)
    assert "NO_SUCH_ROOT" in fl.unresolved_env
    assert any("Unset environment variable" in e for e in fl.errors)
