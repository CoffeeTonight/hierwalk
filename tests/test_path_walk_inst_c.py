"""Path-walk: resolving ``c`` in ``a.b.c.d`` (split decls, arrays, generate-fold)."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import PathWalkState, run_path_walk_connect


def _run(tmp_path: Path, files: dict[str, str], path: str, *, top: str = "A") -> bool:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(str((tmp_path / name).resolve()) for name in files) + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(path, path),),
        top=top,
    )
    _batch, _index, state = run_path_walk_connect(
        request,
        flr,
        top=top,
        no_cache=True,
    )
    return path in state.rows_by_path


def test_inst_leaf_prefix_is_path_segment_not_hier_inst_name():
    """Endpoint remainders use dotted hierarchy paths, not ``inst_scan`` hier refs."""
    assert PathWalkState._inst_leaf_prefix("c") == "c"
    assert PathWalkState._inst_leaf_prefix("c.d") == "c"
    assert PathWalkState._inst_leaf_prefix("c.d.e") == "c"
    assert PathWalkState._inst_leaf_prefix("c[0][1].d") == "c[0][1]"


def test_path_walk_c_only_in_second_b_decl(tmp_path: Path):
    """Other instances in first ``B`` decl; ``c`` only in a later duplicate module."""
    files = {
        "a.v": "module A; B B (); endmodule\n",
        "b1.v": "module B; X x (); Y y (); endmodule\n",
        "b2.v": "module B; X x (); Y y (); C c (); endmodule\n",
        "c.v": "module C; D d (); endmodule\n",
        "d.v": "module D; endmodule\n",
        "x.v": "module X; endmodule\n",
        "y.v": "module Y; endmodule\n",
    }
    assert _run(tmp_path, files, "A.B.c.d")


def test_path_walk_bare_c_requires_array_index(tmp_path: Path):
    """``c[0:1][0:1]`` must be addressed as ``c[i][j]``, not bare ``c``."""
    files = {
        "a.v": "module A; B b (); endmodule\n",
        "b.v": (
            "module B;\n"
            "  md2d_c c[0:1][0:1] ();\n"
            "endmodule\n"
        ),
        "md2d_c.v": "module md2d_c; D d (); endmodule\n",
        "d.v": "module D; endmodule\n",
    }
    assert not _run(tmp_path, files, "A.b.c.d", top="A")
    assert _run(tmp_path, files, "A.b.c[0][1].d", top="A")


def test_path_walk_multiline_ifndef_cpusystem_top(tmp_path: Path):
    """User-style ``ifndef NO_A`` + ``endif//NO_A`` + multiline ``u_cpusystem_top``."""
    files = {
        "soc.v": (
            "module SOC_TOP;\n"
            "////////\n"
            "`ifndef NO_A\n"
            "A u_a\n"
            "(\n"
            ".aa (w_aa));\n"
            "`endif//NO_A\n"
            "CPUSYSTEM_TOP u_cpusystem_top\n"
            "(\n"
            ".clk(clk));\n"
            "endmodule\n"
        ),
        "cpu.v": "module CPUSYSTEM_TOP; endmodule\n",
        "a.v": "module A; endmodule\n",
    }
    assert _run(tmp_path, files, "SOC_TOP.u_cpusystem_top", top="SOC_TOP")


def test_path_walk_endif_label_same_line_cpusystem(tmp_path: Path):
    """`` `endif//NO_A`` label on same line as the next instance must parse."""
    files = {
        "soc.v": (
            "module SOC_TOP;\n"
            "`ifndef NO_A\n"
            "A u_a (.aa(w));\n"
            "`endif//NO_A CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));\n"
            "endmodule\n"
        ),
        "cpu.v": "module CPUSYSTEM_TOP; endmodule\n",
        "a.v": "module A; endmodule\n",
    }
    assert _run(tmp_path, files, "SOC_TOP.u_cpusystem_top", top="SOC_TOP")


def test_instances_for_walk_prefers_tier1_outside_generate(tmp_path: Path):
    """``needs_generate_fold`` must not discard tier-1 instances outside ``generate``."""
    soc = tmp_path / "soc.v"
    soc.write_text(
        "module SOC_TOP;\n"
        "CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));\n"
        "genvar gi;\n"
        "generate\n"
        "  for (gi = 0; gi < 2; gi++) begin : gen_blk\n"
        "    LEAF u_leaf (.clk(clk));\n"
        "  end\n"
        "endgenerate\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb, path_walk_db_cache_key

    fl = tmp_path / "design.f"
    fl.write_text(str(soc.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    db = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        no_cache=True,
    )
    scanned = db.tier1_scan_file(str(soc.resolve()))
    db._apply_file_modules(str(soc.resolve()), scanned)
    rec = index.get_module("SOC_TOP")
    assert rec is not None
    assert rec.needs_generate_fold
    names = {e.inst_name for e in index.instances_for_walk("SOC_TOP", {})}
    assert "u_cpusystem_top" in names


def test_index_tier1_finds_cpusystem_outside_nonempty_generate(tmp_path: Path):
    """Instances outside ``generate`` must appear in tier-1 even when fold is deferred."""
    soc = tmp_path / "soc.v"
    soc.write_text(
        "module SOC_TOP;\n"
        "////////\n"
        "`ifndef NO_A\n"
        "A u_a\n"
        "(\n"
        ".aa (w_aa));\n"
        "`endif//NO_A\n"
        "CPUSYSTEM_TOP u_cpusystem_top\n"
        "(\n"
        ".clk(clk));\n"
        "genvar gi;\n"
        "generate\n"
        "  for (gi = 0; gi < 2; gi++) begin : gen_blk\n"
        "    LEAF u_leaf (.clk(clk));\n"
        "  end\n"
        "endgenerate\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from hierwalk.preprocess import preprocess_file_for_index
    from hierwalk.scan import scan_preprocessed

    text = preprocess_file_for_index(soc, [], {})
    mods = scan_preprocessed(text, str(soc))
    rec = mods["SOC_TOP"]
    assert rec.needs_generate_fold
    names = {e.inst_name for e in rec.instances}
    assert "u_cpusystem_top" in names
    assert "u_a" in names
    assert not any(e.inst_name.startswith("gen_blk") for e in rec.instances)


def test_path_walk_combined_ifdef_comment_generate_param_override(tmp_path: Path):
    """Nested ``ifdef``/``ifndef``/``elsif`` + comments + generate + ``#(.a(2),.b(2-1))``."""
    files = {
        "soc.v": (
            "module SOC_TOP;\n"
            "////////\n"
            "// `endif trap in line comment\n"
            "`ifndef ASD\n"
            "A u_A\n"
            "(\n"
            ".aa (w_aa));\n"
            "`elsif USE_B\n"
            "B u_B (.bb(w_bb));\n"
            "`else\n"
            "STUB u_stub ();\n"
            "`endif//ASD\n"
            "/*\n"
            "`ifdef FAKE\n"
            "FAKE u_fake ();\n"
            "`endif\n"
            "*/\n"
            "CPUSYSTEM_TOP u_cpusystem_top\n"
            "(\n"
            ".clk(clk));\n"
            "BCD #(.a(2),.b(2-1)) u_BCD (.clk(clk));\n"
            "genvar gi;\n"
            "generate\n"
            "  for (gi = 0; gi < 2; gi++) begin : gen_blk\n"
            "`ifdef GEN_LEAF\n"
            "    LEAF #(.idx(gi)) u_leaf (.clk(clk)); // `endif in comment\n"
            "`elsif GEN_ALT\n"
            "    ALT u_alt ();\n"
            "`else\n"
            "    BCD #(.a(gi),.b(2-1)) u_BCD_gen (.clk(clk));\n"
            "`endif\n"
            "  end\n"
            "endgenerate\n"
            "endmodule\n"
        ),
        "cpu.v": "module CPUSYSTEM_TOP; endmodule\n",
        "a.v": "module A; endmodule\n",
        "b.v": "module B; endmodule\n",
        "stub.v": "module STUB; endmodule\n",
        "bcd.v": "module BCD; endmodule\n",
        "leaf.v": "module LEAF; endmodule\n",
        "alt.v": "module ALT; endmodule\n",
    }
    assert _run(tmp_path, files, "SOC_TOP.u_A", top="SOC_TOP")
    assert _run(tmp_path, files, "SOC_TOP.u_cpusystem_top", top="SOC_TOP")
    assert _run(tmp_path, files, "SOC_TOP.u_BCD", top="SOC_TOP")
    assert _run(tmp_path, files, "SOC_TOP.gen_blk[0].u_BCD_gen", top="SOC_TOP")
    assert _run(tmp_path, files, "SOC_TOP.gen_blk[1].u_BCD_gen", top="SOC_TOP")


def test_path_walk_generate_fold_child_before_index_apply(tmp_path: Path):
    """Tier-1 edge resolve must fold generate before matching folded inst names."""
    files = {
        "top.v": "module top; mid mid (); endmodule\n",
        "mid.v": (
            "module mid (input logic clk);\n"
            "  genvar gi;\n"
            "  generate\n"
            "    for (gi = 0; gi < 2; gi++) begin : gen_loop\n"
            "      leaf u ( .clk(clk) );\n"
            "    end\n"
            "  endgenerate\n"
            "endmodule\n"
        ),
        "leaf.v": "module leaf (input logic clk); endmodule\n",
    }
    path = "top.mid.gen_loop[0].u"
    assert _run(tmp_path, files, path, top="top")