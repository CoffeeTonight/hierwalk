"""Preprocess directive order: ifdef, define, include, undef, elsif."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_scan import collect_design_defines, prepare_connect_body
from hierwalk.index import DesignIndex, scan_preprocessed
from hierwalk.preprocess import (
    apply_ifdef_filter,
    clear_include_unit_cache,
    preprocess_file,
    preprocess_file_for_index,
)


def test_ifndef_define_guard_external_define_skips_module(tmp_path: Path):
    rtl = tmp_path / "bla.v"
    rtl.write_text(
        "`ifndef _BLA_\n"
        "`define _BLA_\n"
        "module BLA(input logic a);\n"
        "endmodule\n"
        "`endif\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file(rtl, [tmp_path], {"_BLA_": "1"})
    assert "module BLA" not in text


def test_elsif_define_only_active_branch(tmp_path: Path):
    src = (
        "`define PICK 1\n"
        "`ifdef USE_A\n"
        "`define X 1\n"
        "`elsif PICK\n"
        "`define X 2\n"
        "`else\n"
        "`define X 3\n"
        "`endif\n"
        "module top;\n"
        "`ifdef X\n"
        "  leaf u_l ();\n"
        "`endif\n"
        "endmodule\n"
        "module leaf; endmodule\n"
    )
    rtl = tmp_path / "top.v"
    rtl.write_text(src, encoding="utf-8")
    clear_include_unit_cache()
    defs: dict[str, str] = {}
    text = preprocess_file(rtl, [tmp_path], defs)
    mods = scan_preprocessed(text, str(rtl))
    assert "u_l" in {e.inst_name for e in mods["top"].instances}
    assert defs.get("X") == "2"


def test_collect_design_defines_honors_rtl_undef(tmp_path: Path):
    (tmp_path / "a.v").write_text("`define FOO 1\n", encoding="utf-8")
    (tmp_path / "b.v").write_text(
        "`undef FOO\n"
        "module top;\n"
        "`ifdef FOO\n"
        "  ghost u_g ();\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(
        "\n".join(
            [
                str((tmp_path / "a.v").resolve()),
                str((tmp_path / "b.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    from hierwalk.filelist import parse_filelist

    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    defs = collect_design_defines(index, sources=[str(p) for p in flr.source_files])
    assert "FOO" not in defs


def test_collect_design_defines_rtl_undef_overrides_filelist(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "`undef FOO\n"
        "module top;\n"
        "`ifdef FOO\n"
        "  ghost u_g ();\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(tmp_path)],
        preprocess_defines={"FOO": "1"},
    )
    defs = collect_design_defines(
        index,
        sources=[str(rtl.resolve())],
        extra_defines={"FOO": "1"},
    )
    assert "FOO" not in defs


def test_undef_before_ifdef(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "`undef FOO\n"
        "`ifdef FOO\n"
        "  ghost u_g ();\n"
        "`endif\n"
        "endmodule\n"
        "module ghost; endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file(rtl, [tmp_path], {"FOO": "1"})
    assert "u_g" not in text


def test_include_only_in_active_ifdef_branch(tmp_path: Path):
    ghost = tmp_path / "ghost.vh"
    ghost.write_text(
        "`define GHOST 1\n"
        "module ghost; endmodule\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "`define OFF 1\n"
        "module top;\n"
        "`ifndef OFF\n"
        '`include "ghost.vh"\n'
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    defs: dict[str, str] = {}
    text = preprocess_file(rtl, [tmp_path], defs)
    assert "GHOST" not in defs
    assert "module ghost" not in text


def test_include_define_affects_parent_ifdef(tmp_path: Path):
    inc = tmp_path / "cfg.vh"
    inc.write_text(
        "`define USE_PCIE 1\n"
        "`ifdef USE_PCIE\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "top.v"
    rtl.write_text(
        '`include "cfg.vh"\n'
        "module top;\n"
        "  pcie u_p ();\n"
        "`else\n"
        "  uart u_u ();\n"
        "`endif\n"
        "endmodule\n"
        "module pcie; endmodule\n"
        "module uart; endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file(rtl, [tmp_path], {})
    mods = scan_preprocessed(text, str(rtl))
    assert any(e.child_module == "pcie" for e in mods["top"].instances)
    assert not any(e.child_module == "uart" for e in mods["top"].instances)


def test_collect_design_defines_seeds_filelist_macros(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "`ifdef FEATURE\n"
        "`define INTERNAL 1\n"
        "  leaf u_l ();\n"
        "`endif\n"
        "endmodule\n"
        "module leaf; endmodule\n",
        encoding="utf-8",
    )
    text = preprocess_file_for_index(rtl, [tmp_path], {"FEATURE": "1"}, apply_ifdef=True)
    index = DesignIndex.build({str(rtl): text})
    index._preprocess_include_dirs = [str(tmp_path)]
    index._preprocess_defines = {"FEATURE": "1"}
    defs = collect_design_defines(index)
    assert defs.get("FEATURE") == "1"
    assert defs.get("INTERNAL") == "1"


def test_collect_design_defines_drops_ifndef_define_include_guards(tmp_path: Path):
    rtl = tmp_path / "guard.v"
    rtl.write_text(
        "`ifndef _BLA_\n"
        "`define _BLA_\n"
        "module BLA(input logic a);\n"
        "`define EXPORTED 1\n"
        "endmodule\n"
        "`endif\n",
        encoding="utf-8",
    )
    text = preprocess_file_for_index(rtl, [tmp_path], {}, apply_ifdef=True)
    index = DesignIndex.build({str(rtl): text})
    index._preprocess_include_dirs = [str(tmp_path)]
    defs = collect_design_defines(index)
    assert "_BLA_" not in defs
    assert defs.get("EXPORTED") == "1"


def test_collect_design_defines_respects_inactive_branch(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "`ifdef DISABLED\n"
        "`define GHOST 1\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    text = preprocess_file_for_index(rtl, [tmp_path], {}, apply_ifdef=True)
    index = DesignIndex.build({str(rtl): text})
    index._preprocess_include_dirs = [str(tmp_path)]
    defs = collect_design_defines(index)
    assert "GHOST" not in defs


def test_macro_body_ifdef_reprocessed(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "`define BLOCK `ifdef ON` secret u_s (); `endif\n"
        "module top;\n"
        "`ifdef USE_BLK\n"
        "  `BLOCK\n"
        "`endif\n"
        "endmodule\n"
        "module secret; endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file(rtl, [tmp_path], {"USE_BLK": "1"})
    assert "u_s" not in text
    text_on = preprocess_file(rtl, [tmp_path], {"USE_BLK": "1", "ON": "1"})
    assert "u_s" in text_on


def test_index_inline_ifdef_define_state(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "`define EN 1\n"
        "`ifdef EN `define TAG 1 `endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    defs: dict[str, str] = {}
    text = preprocess_file_for_index(rtl, [tmp_path], defs)
    assert "TAG" in defs
    out = apply_ifdef_filter(text, defs)
    assert "module top" in out


def test_prepare_connect_body_param_after_ifdef():
    body = (
        "parameter N = 2;\n"
        "`ifdef OFF\n"
        "parameter M = 9;\n"
        "`endif\n"
        "  mem u_b [N:0] ( );\n"
    )
    text_active = prepare_connect_body(body, defines={"OFF": "1"})
    assert "parameter M = 9" in text_active
    text_inactive = prepare_connect_body(body, defines={})
    assert "parameter M" not in text_inactive