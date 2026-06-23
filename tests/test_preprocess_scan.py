"""Preprocessor + instance scan tests."""

from __future__ import annotations

from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.preprocess import (
    _INCLUDE_UNIT_CACHE,
    _collect_include_closure,
    apply_ifdef_filter,
    clear_include_unit_cache,
    preprocess_file,
    preprocess_sources,
    strip_comments,
)
from hierwalk.scan import flatten, scan_preprocessed


def test_strip_comments():
    t = "a // c\n/* x */ b"
    assert strip_comments(t) == "a \n b"


def test_strip_comments_slash_inside_line_comment_not_block():
    """``/*`` after ``//`` on a line must not open a block (even across lines)."""
    src = "code // foo /* bar\ncontinues */\nmore"
    assert strip_comments(src) == "code \ncontinues */\nmore"

    from hierwalk.preprocess import strip_comments_for_instance_scan

    rtl = (
        "module top;\n"
        "  keep u_keep ();\n"
        "  // doc /* fake block\n"
        "  */\n"
        "  tail u_tail ();\n"
        "endmodule\n"
    )
    cleaned = strip_comments_for_instance_scan(rtl)
    assert "u_keep" in cleaned
    assert "u_tail" in cleaned
    assert "fake block" not in cleaned
    mods = scan_preprocessed(cleaned, "top.v")
    names = {e.inst_name for e in mods["top"].instances}
    assert names == {"u_keep", "u_tail"}


def test_ifdef_filter_single_line():
    src = "`ifdef GHOST assign link=src; `else assign link=1'b0; `endif"
    off = apply_ifdef_filter(src, {})
    assert off == "assign link=1'b0;"
    on = apply_ifdef_filter(src, {"GHOST": "1"})
    assert on == "assign link=src;"


def test_ifdef_filter_ignores_directives_in_line_comments():
    # `` `endif `` inside ``//`` must not pop the ifdef stack early.
    src = (
        "`ifndef NO_CPU\n"
        "  CPUSYSTEM_TOP u_cpusystem_top (); // `endif\n"
        "  B u_b ();\n"
        "`endif\n"
    )
    out = apply_ifdef_filter(src, {})
    assert "u_cpusystem_top" in out
    assert "u_b" in out

    src_else_trap = (
        "`ifdef USE_A\n"
        "  A u_a ();\n"
        "// `else\n"
        "  C u_c ();\n"
        "`else\n"
        "  B u_b ();\n"
        "`endif\n"
    )
    out_a = apply_ifdef_filter(src_else_trap, {"USE_A": "1"})
    assert "u_a" in out_a
    assert "u_c" in out_a
    assert "u_b" not in out_a
    out_b = apply_ifdef_filter(src_else_trap, {"USE_A": "0"})
    assert "u_b" in out_b
    assert "u_a" not in out_b


def test_ifdef_filter_ignores_directives_in_block_comments():
    src = """
A u_a ();
/*
`ifdef NO_A
B u_b ();
`endif
*/
C u_c ();
"""
    out = apply_ifdef_filter(src, {"NO_A": "1"})
    assert "u_a" in out
    assert "u_c" in out
    assert "u_b" not in out
    assert "/*" not in out
    assert "*/" not in out


def test_ifdef_filter_preserves_rtl_after_endif_label_comment():
    """`` `endif//MACRO`` label comments must not swallow same-line RTL."""
    src = (
        "module top;\n"
        "`ifndef NO_A\n"
        "A u_a (.aa(1'b0));\n"
        "`endif//NO_A CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));\n"
        "endmodule\n"
    )
    out = apply_ifdef_filter(src, {})
    assert "u_a" in out
    assert "u_cpusystem_top" in out
    out_def = apply_ifdef_filter(src, {"NO_A": "1"})
    assert "u_a" not in out_def
    assert "u_cpusystem_top" in out_def


def test_slim_body_preserves_rtl_after_endif_label_comment():
    from hierwalk.inst_scan import scan_hierarchy_instances

    body = (
        "`ifndef NO_A\n"
        "A u_a (.aa(1'b0));\n"
        "`endif//NO_A CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));\n"
    )
    edges = scan_hierarchy_instances(body)
    names = {e.inst_name for e in edges}
    assert "u_a" in names
    assert "u_cpusystem_top" in names


def test_ifdef_filter_skips_slash_comment_lines_before_ifdef():
    src = """
////////
`ifdef NO_A
  A u_a ();
`endif
"""
    out = apply_ifdef_filter(src, {"NO_A": "1"})
    assert "////////" not in out
    assert "u_a" in out


def test_ifdef_filter():
    src = """
`ifdef USE_A
  child_a u_a ();
`else
  child_b u_b ();
`endif
"""
    on = apply_ifdef_filter(src, {"USE_A": "1"})
    assert "u_a" in on and "u_b" not in on
    off = apply_ifdef_filter(src, {"USE_A": "0"})
    assert "u_b" in off and "u_a" not in off


def test_collect_include_closure_skip_count(tmp_path: Path):
    ignored_dir = tmp_path / "pcielinktop"
    ignored_dir.mkdir()
    (ignored_dir / "skip.vh").write_text("`define X 1\n", encoding="utf-8")
    keep = tmp_path / "keep.vh"
    keep.write_text("`define Y 1\n", encoding="utf-8")
    src = tmp_path / "top.v"
    src.write_text(
        '`include "pcielinktop/skip.vh"\n'
        '`include "keep.vh"\n'
        "module top; endmodule\n",
        encoding="utf-8",
    )
    closure, skipped = _collect_include_closure(
        [src],
        [tmp_path],
        skip_path_patterns=["pcielinktop"],
    )
    assert len(closure) == 1
    assert closure[0].resolve() == keep.resolve()
    assert skipped == 1


def test_preprocess_warms_shared_includes(tmp_path: Path):
    inc = tmp_path / "shared.vh"
    inc.write_text("`define SHARED_FLAG 1\n", encoding="utf-8")
    sources = []
    for i in range(8):
        p = tmp_path / f"m_{i}.v"
        p.write_text(f'`include "shared.vh"\nmodule m_{i}; endmodule\n', encoding="utf-8")
        sources.append(str(p))
    clear_include_unit_cache()
    preprocess_sources(sources, [tmp_path], {}, jobs=1)
    assert any("shared.vh" in k[0] for k in _INCLUDE_UNIT_CACHE)


def test_include_and_define(tmp_path: Path):
    inc = tmp_path / "cfg.vh"
    inc.write_text(
        "`define USE_PCIE 1\n`ifdef USE_PCIE\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "`include \"cfg.vh\"\n"
        "module top;\n"
        "  pcie u_p ();\n"
        "`else\n"
        "  uart u_u ();\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [tmp_path], {"USE_PCIE": "1"})
    mods = scan_preprocessed(text, str(rtl))
    assert "top" in mods
    assert any(e.child_module == "pcie" for e in mods["top"].instances)
    assert not any(e.child_module == "uart" for e in mods["top"].instances)


def test_bind_skipped(tmp_path: Path):
    rtl = tmp_path / "t.v"
    rtl.write_text(
        "module top;\n"
        "  cpu u_c ();\n"
        "endmodule\n"
        "bind top extra u_e ();\n"
        "module cpu; endmodule\n"
        "module extra; endmodule\n",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    paths = flatten(mods, "top")
    assert [r.full_path for r in paths] == ["top", "top.u_c"]


def test_filelist_nested_f_lowercase(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "child.v").write_text("module child; endmodule\n", encoding="utf-8")
    (tmp_path / "top.v").write_text(
        "module top;\n  child u_c ();\nendmodule\n",
        encoding="utf-8",
    )
    (sub / "sub.f").write_text("child.v\n", encoding="utf-8")
    top_f = tmp_path / "top.f"
    top_f.write_text(f"-f sub/sub.f\ntop.v\n", encoding="utf-8")
    fl = parse_filelist(str(top_f))
    assert len(fl.source_files) == 2


def test_filelist_nested_F_uppercase(tmp_path: Path):
    """-F: nested from index_cwd, inner paths from index_cwd too."""
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "child.v").write_text("module child; endmodule\n", encoding="utf-8")
    (rtl / "top.v").write_text(
        "module top;\n  child u_c ();\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "nested.f").write_text("rtl/child.v\n", encoding="utf-8")
    top_f = tmp_path / "top.f"
    top_f.write_text("-F nested.f\nrtl/top.v\n", encoding="utf-8")
    fl = parse_filelist(str(top_f), index_cwd=str(tmp_path))
    assert len(fl.source_files) == 2


def test_preprocess_cache_hit_replays_in_file_defines_for_ifdef():
    """Second tier-1 preprocess must not drop `` `define `` state on cache hit."""
    from hierwalk.preprocess import preprocess_file_for_index
    from hierwalk.scan import scan_preprocessed

    clear_include_unit_cache()
    src = (
        "`define USE_CPU 0\n"
        "module top;\n"
        "`ifdef USE_CPU\n"
        "  CPUSYSTEM_TOP u_cpusystem_top ();\n"
        "`endif\n"
        "endmodule\n"
    )
    tmp = Path("/tmp/hierwalk_preprocess_cache_ifdef.v")
    tmp.write_text(src, encoding="utf-8")

    defs1: dict[str, str] = {}
    text1 = preprocess_file_for_index(tmp, [], defs1)
    out1 = apply_ifdef_filter(text1, defs1)
    insts1 = [e.inst_name for e in scan_preprocessed(out1, str(tmp))["top"].instances]
    assert insts1 == []

    defs2: dict[str, str] = {}
    text2 = preprocess_file_for_index(tmp, [], defs2)
    out2 = apply_ifdef_filter(text2, defs2)
    insts2 = [e.inst_name for e in scan_preprocessed(out2, str(tmp))["top"].instances]
    assert defs2 == {"USE_CPU": "0"}
    assert insts2 == []


def test_end_to_end_cli(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.v").write_text(
        "module top;\n  sub u_s ();\nendmodule\nmodule sub;\n  leaf u_l ();\nendmodule\nmodule leaf; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "d.f"
    fl.write_text(f"{tmp_path / 'a.v'}\n", encoding="utf-8")
    out = tmp_path / ".db_top" / "out.tsv"
    from hierwalk.cli import main

    assert main([str(fl), "--top", "top", "-o", str(out), "--max-depth", "2"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "top.u_s" in text
    assert "top.u_s.u_l" in text