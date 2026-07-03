"""Preprocess directive order: ifdef, define, include, undef, elsif."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.logical.scan import collect_design_defines, design_parse_sources, prepare_connect_body
from hierwalk.index import DesignIndex, scan_preprocessed
from hierwalk.preprocess import (
    accumulate_defines_from_file,
    apply_ifdef_filter,
    clear_include_unit_cache,
    define_snapshots_for_sources,
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


def test_collect_design_defines_preserves_seeded_names_against_include_guards(
    tmp_path: Path,
):
    """Filelist/request defines must survive guard pop from shared preamble RTL."""
    common = tmp_path / "zz_common.v"
    common.write_text(
        "`ifndef ZZ_TORTURE\n"
        "`define ZZ_TORTURE 1\n"
        "`endif\n"
        "`ifndef ZZ_REAL_IFDEF\n"
        "`define ZZ_REAL_IFDEF 1\n"
        "`endif\n"
        "module zz_leaf; endmodule\n",
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text("module top; zz_leaf u (); endmodule\n", encoding="utf-8")
    sources = [str(common.resolve()), str(top.resolve())]
    seed = {"ZZ_TORTURE": "1", "ZZ_REAL_IFDEF": "1"}
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(tmp_path)],
        preprocess_defines=dict(seed),
        parse_sources=sources,
    )
    defs = collect_design_defines(index, sources=sources)
    assert defs.get("ZZ_TORTURE") == "1"
    assert defs.get("ZZ_REAL_IFDEF") == "1"


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


def test_collect_design_defines_define_only_file(tmp_path: Path):
    (tmp_path / "macros.v").write_text("`define FEATURE 1\n", encoding="utf-8")
    (tmp_path / "top.v").write_text(
        "module top;\n"
        "`ifdef FEATURE\n"
        "  leaf u_l ();\n"
        "`endif\n"
        "endmodule\n"
        "module leaf; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(
        "\n".join(
            [
                str((tmp_path / "macros.v").resolve()),
                str((tmp_path / "top.v").resolve()),
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
        preprocess_include_dirs=[str(tmp_path)],
        preprocess_defines={},
        parse_sources=[str(p) for p in flr.source_files],
    )
    defs = collect_design_defines(index, sources=[str(p) for p in flr.source_files])
    assert defs.get("FEATURE") == "1"


def test_collect_design_defines_preserves_filelist_order(tmp_path: Path):
    (tmp_path / "first.v").write_text("`define FOO 1\n", encoding="utf-8")
    (tmp_path / "second.v").write_text("`undef FOO\n", encoding="utf-8")
    sources = [
        str((tmp_path / "second.v").resolve()),
        str((tmp_path / "first.v").resolve()),
    ]
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(tmp_path)],
        preprocess_defines={},
        parse_sources=sources,
    )
    defs = collect_design_defines(index, sources=sources)
    assert "FOO" in defs


def test_include_guard_with_blank_line_between(tmp_path: Path):
    rtl = tmp_path / "guard.v"
    rtl.write_text(
        "`ifndef _GUARD_\n"
        "\n"
        "`define _GUARD_\n"
        "module m; endmodule\n"
        "`endif\n",
        encoding="utf-8",
    )
    from hierwalk.preprocess import include_guard_macro_names

    assert "_GUARD_" in include_guard_macro_names(rtl.read_text(encoding="utf-8"))


def test_prepare_connect_body_expands_include_with_source_file(tmp_path: Path):
    inc = tmp_path / "cfg.vh"
    inc.write_text("`define ON 1\n", encoding="utf-8")
    rtl = tmp_path / "top.v"
    rtl.write_text(
        '`include "cfg.vh"\n'
        "module top;\n"
        "`ifdef ON\n"
        "  wire active;\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    body = "module top;\n`include \"cfg.vh\"\n`ifdef ON\n  wire active;\n`endif\nendmodule\n"
    text = prepare_connect_body(
        body,
        defines={},
        source_file=str(rtl),
        include_dirs=[str(tmp_path)],
    )
    assert "wire active" in text


def test_preprocess_sources_serial_honors_cross_file_undef(tmp_path: Path):
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
    from hierwalk.preprocess import clear_include_unit_cache, preprocess_sources

    clear_include_unit_cache()
    sources = [
        str((tmp_path / "a.v").resolve()),
        str((tmp_path / "b.v").resolve()),
    ]
    out = preprocess_sources(sources, [tmp_path], {}, jobs=1)
    text = out[str((tmp_path / "b.v").resolve())]
    assert "u_g" not in text


def test_include_guard_linear_on_large_gap():
    from hierwalk.preprocess import include_guard_macro_names

    text = "`ifndef BIG_GUARD_\n" + "// filler\n" * 50_000 + "`define BIG_GUARD_\n"
    assert "BIG_GUARD_" in include_guard_macro_names(text)


def test_include_guard_with_block_comment_between(tmp_path: Path):
    rtl = tmp_path / "guard.v"
    rtl.write_text(
        "`ifndef _GUARD_\n"
        "/* block */\n"
        "`define _GUARD_\n"
        "module m; endmodule\n"
        "`endif\n",
        encoding="utf-8",
    )
    from hierwalk.preprocess import include_guard_macro_names

    assert "_GUARD_" in include_guard_macro_names(rtl.read_text(encoding="utf-8"))


def test_ports_for_design_module_hides_ifdef_gated_port(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "`ifdef OFF\n"
        "  input logic hidden_port;\n"
        "`endif\n"
        "  input logic visible_port;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from hierwalk.index import DesignIndex
    from hierwalk.port_scan import ports_for_design_module

    index = DesignIndex.build_from_sources(
        [str(rtl.resolve())],
        include_dirs=[str(tmp_path)],
        defines={},
        jobs=1,
        low_memory=True,
    )
    ports = ports_for_design_module(index, "top", {})
    assert "visible_port" in ports
    assert "hidden_port" not in ports


def test_design_parse_sources_infers_module_first_seen_order(tmp_path: Path):
    from hierwalk.models import ModuleRecord

    z_path = str((tmp_path / "z.v").resolve())
    a_path = str((tmp_path / "a.v").resolve())
    index = DesignIndex(
        {
            "z_mod": ModuleRecord(module_name="z_mod", file_path=z_path, body=""),
            "a_mod": ModuleRecord(module_name="a_mod", file_path=a_path, body=""),
        }
    )
    assert design_parse_sources(index) == [z_path, a_path]
    assert index._parse_sources == [z_path, a_path]


def test_patch_files_honors_cross_file_undef(tmp_path: Path):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a_path = str(a.resolve())
    b_path = str(b.resolve())
    b.write_text(
        "module top;\n"
        "`ifdef FOO\n"
        "  ghost u_g ();\n"
        "`endif\n"
        "endmodule\n"
        "module ghost; endmodule\n",
        encoding="utf-8",
    )
    index = DesignIndex.build_from_sources(
        [b_path],
        include_dirs=[str(tmp_path)],
        defines={"FOO": "1"},
        jobs=1,
        low_memory=True,
    )
    top = index.get_module("top")
    assert top is not None
    assert any(e.inst_name == "u_g" for e in top.instances)

    a.write_text("`define FOO 1\n", encoding="utf-8")
    b.write_text(
        "`undef FOO\n"
        "module top;\n"
        "`ifdef FOO\n"
        "  ghost u_g ();\n"
        "`endif\n"
        "endmodule\n"
        "module ghost; endmodule\n",
        encoding="utf-8",
    )
    index._parse_sources = [a_path, b_path]
    index.patch_files(
        [b_path],
        [],
        include_dirs=[str(tmp_path)],
        defines={},
        jobs=1,
    )
    top = index.get_module("top")
    assert top is not None
    assert not any(e.inst_name == "u_g" for e in top.instances)


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


def test_accumulate_defines_matches_light_preprocess(tmp_path: Path):
    inc = tmp_path / "cfg.vh"
    inc.write_text(
        "`define USE_PCIE 1\n"
        "`ifdef USE_PCIE\n",
        encoding="utf-8",
    )
    (tmp_path / "macros.v").write_text("`define FEATURE 1\n", encoding="utf-8")
    rtl = tmp_path / "top.v"
    rtl.write_text(
        '`include "cfg.vh"\n'
        "module top;\n"
        "`ifdef FEATURE\n"
        "`define TAG 1\n"
        "`endif\n"
        "`else\n"
        "`define TAG 0\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    sources = [
        str((tmp_path / "macros.v").resolve()),
        str(rtl.resolve()),
    ]
    clear_include_unit_cache()

    light: dict[str, str] = {}
    heavy: dict[str, str] = {}
    for src in sources:
        path = Path(src)
        guards = set()
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            raw = ""
        if raw:
            from hierwalk.preprocess import include_guard_macro_names

            guards = include_guard_macro_names(raw)
        light_run = dict(light)
        accumulate_defines_from_file(
            path,
            light_run,
            [tmp_path],
            set(),
            apply_ifdef=True,
        )
        for name in guards:
            light_run.pop(name, None)
        light = light_run

        heavy_run = dict(heavy)
        preprocess_file_for_index(
            path,
            [tmp_path],
            heavy_run,
            set(),
            apply_ifdef=True,
        )
        for name in guards:
            heavy_run.pop(name, None)
        heavy = heavy_run

    assert light == heavy
    assert light.get("FEATURE") == "1"
    assert light.get("TAG") == "1"

    snaps = define_snapshots_for_sources(
        sources,
        include_dirs=[tmp_path],
        base_defines={},
    )
    assert snaps[sources[0]] == ()
    assert snaps[sources[1]] == (("FEATURE", "1"),)