"""Synthesizable instance definition forms."""

from __future__ import annotations

from hierwalk.index import DesignIndex
from hierwalk.inst_scan import scan_hierarchy_instances
from hierwalk.preprocess import preprocess_file
from hierwalk.generate_fold import needs_generate_fold, prepare_body_for_instance_scan
from hierwalk.scan import flatten, scan_preprocessed


def _index_instances(text: str, rtl_path, mod_name: str = "top"):
    index = DesignIndex.build({str(rtl_path): text})
    return index.instances_for(mod_name, {}, {})


def test_param_before_inst():
    body = "parameter N = 4;\n  child #( .W(8) ) u0 ( );\n  child2 u1;\n"
    edges = scan_hierarchy_instances(body, param_map={"N": "4"})
    assert ("u0", "child") in [(e.inst_name, e.child_module) for e in edges]
    assert ("u1", "child2") in [(e.inst_name, e.child_module) for e in edges]


def test_array_literal_expand():
    body = "  mem u_arr [3:0] ( );\n"
    edges = scan_hierarchy_instances(body)
    names = [e.inst_name for e in edges]
    assert names == ["u_arr[0]", "u_arr[1]", "u_arr[2]", "u_arr[3]"]


def test_param_array_expand():
    body = "parameter N = 2;\n  mem u_b [N:0] ( );\n"
    edges = scan_hierarchy_instances(body, param_map={"N": "2"})
    names = [e.inst_name for e in edges]
    assert names == ["u_b[0]", "u_b[1]", "u_b[2]"]


def test_comma_separated_instances():
    body = "  child u1 (a,b,c), u2 (d,e,f);\n"
    edges = scan_hierarchy_instances(body)
    assert len(edges) == 2
    assert {e.inst_name for e in edges} == {"u1", "u2"}


def test_needs_generate_fold_skips_plain_modules():
    plain = "  child u0 ( );\n  assign x = y;\n"
    assert not needs_generate_fold(plain)
    assert prepare_body_for_instance_scan(plain, {}) is plain


def test_needs_generate_fold_ignores_comment_and_empty_generate():
    body = "// generate loop\n  child u0 ( );\n  generate\n  endgenerate\n"
    assert not needs_generate_fold(body)
    body2 = "genvar gi;\n  child u0 ( );\n  generate\n  endgenerate\n"
    assert not needs_generate_fold(body2)


def test_index_defers_generate_fold_to_instances_for(tmp_path):
    rtl = tmp_path / "g.v"
    rtl.write_text(
        """
module top;
  generate
    for (genvar i=0; i<2; i++) begin : g
      child u_c ( );
    end
  endgenerate
endmodule
module child; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    assert mods["top"].instances == []
    from hierwalk.elab import elaborate

    index = DesignIndex.build({str(rtl): text})
    assert index.modules["top"].needs_generate_fold
    edges = index.instances_for("top", {}, {})
    assert len(edges) == 2
    _, rows = elaborate(index, "top")
    assert any(r.full_path.startswith("top.g[") for r in rows)


def test_generate_block_instances(tmp_path):
    rtl = tmp_path / "g.v"
    rtl.write_text(
        """
module top;
  generate
    for (genvar i=0; i<2; i++) begin : g
      leaf u_l ( );
    end
  endgenerate
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    rows = flatten(mods, "top", max_depth=4)
    paths = {r.full_path for r in rows}
    assert paths >= {"top.g[0].u_l", "top.g[1].u_l"}


def test_generate_labeled_if_for_hier_path(tmp_path):
    rtl = tmp_path / "gen_hier.v"
    rtl.write_text(
        """
module top;
  generate
    if (1) begin : gen_blk
      for (gi = 0; gi < 2; gi++) begin : gen_loop
        leaf_cell u_cell ( );
      end
    end
  endgenerate
endmodule
module leaf_cell; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    rows = flatten(mods, "top", max_depth=4)
    paths = {r.full_path for r in rows}
    assert paths >= {
        "top.gen_blk.gen_loop[0].u_cell",
        "top.gen_blk.gen_loop[1].u_cell",
    }


def test_generate_if_param(tmp_path):
    rtl = tmp_path / "ifgen.v"
    rtl.write_text(
        """
module top;
  parameter N_PE = 2;
  generate
    if (N_PE) begin
      a_mod u_a ( );
    end else begin
      b_mod u_b ( );
    end
  endgenerate
endmodule
module a_mod; endmodule
module b_mod; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    insts = _index_instances(text, rtl)
    assert any(e.child_module == "a_mod" for e in insts)
    assert not any(e.child_module == "b_mod" for e in insts)


def test_generate_for_unroll_three(tmp_path):
    rtl = tmp_path / "forgen.v"
    rtl.write_text(
        """
module top;
  generate
    for (genvar i=0; i<3; i++) begin
      leaf u_arr [i] ( );
    end
  endgenerate
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    names = sorted(e.inst_name for e in _index_instances(text, rtl))
    assert names == ["u_arr[0]", "u_arr[1]", "u_arr[2]"]


def test_generate_for_array_index(tmp_path):
    rtl = tmp_path / "arrgen.v"
    rtl.write_text(
        """
module top;
  generate
    for (genvar i=0; i<2; i++) begin
      mem u_arr [i] ( );
    end
  endgenerate
endmodule
module mem; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    names = [e.inst_name for e in _index_instances(text, rtl)]
    assert names == ["u_arr[0]", "u_arr[1]"]


def test_ifdef_generate_branch(tmp_path):
    rtl = tmp_path / "ifdefgen.v"
    rtl.write_text(
        """
module top;
  generate
`ifdef GEN_PCIE
    pcie u_p ( );
`elsif GEN_USB
    usb u_u ( );
`else
    stub u_s ( );
`endif
  endgenerate
endmodule
module pcie; endmodule
module usb; endmodule
module stub; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {"GEN_PCIE": "1"})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    insts = _index_instances(text, rtl)
    assert any(e.child_module == "pcie" for e in insts)
    assert not any(e.child_module == "usb" for e in insts)


def test_localparam_drives_generate_for(tmp_path):
    rtl = tmp_path / "lp.v"
    rtl.write_text(
        """
module top;
  parameter N = 2;
  localparam LP = N * 2;
  generate
    for (genvar i=0; i<LP; i++) begin
      leaf u_arr [i] ( );
    end
  endgenerate
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    assert mods["top"].needs_generate_fold
    names = sorted(e.inst_name for e in _index_instances(text, rtl))
    assert names == ["u_arr[0]", "u_arr[1]", "u_arr[2]", "u_arr[3]"]


def test_parent_param_override_child_generate(tmp_path):
    rtl = tmp_path / "inherit.v"
    rtl.write_text(
        """
module child #(parameter N = 1) ();
  localparam LP = N + 1;
  generate
    if (LP > 1) begin
      a_mod u_a ( );
    end else begin
      b_mod u_b ( );
    end
  endgenerate
endmodule
module parent;
  parameter P = 2;
  child #( .N(P) ) u_c ( );
endmodule
module a_mod; endmodule
module b_mod; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    rows = flatten(mods, "parent", max_depth=4)
    child_rows = [r for r in rows if r.full_path.startswith("parent.u_c.")]
    assert any(r.module == "a_mod" for r in child_rows)
    assert not any(r.module == "b_mod" for r in child_rows)


def test_header_parameter_default(tmp_path):
    rtl = tmp_path / "hdr.v"
    rtl.write_text(
        """
module child #(parameter N = 3) ();
  generate
    for (genvar i=0; i<N; i++) begin
      leaf u_arr [i] ( );
    end
  endgenerate
endmodule
module top;
  child u0 ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    rows = flatten(mods, "top", max_depth=4)
    leaves = [r.full_path for r in rows if r.module == "leaf"]
    assert leaves == [
        "top.u0.u_arr[0]",
        "top.u0.u_arr[1]",
        "top.u0.u_arr[2]",
    ]


def test_nested_param_override_parens():
    from hierwalk.params import parse_param_overrides

    ov = parse_param_overrides(".test(0.0), x(1+2*(1+TWO)+1), z(TWO)")
    assert ov["test"] == "0.0"
    assert ov["x"] == "1+2*(1+TWO)+1"
    assert ov["z"] == "TWO"


def test_hdlforast_middle_instances(tmp_path):
    """Regression against regexVerilogAST HDLforAST middle_module.v."""
    rtl = tmp_path / "middle_module.v"
    rtl.write_text(
        """
module middle_module #(parameter ONE=1)( input clk, input reset, output out );
localparam TWO = 2;
  sub_module u_subTop_0 #(.test(0.0), x(1+2*(1+TWO)+1), z(TWO)) (
    .clk(clk), .reset(reset), .out(out)
  );
  sub_module u_sub_1 ( .clk(clk), .reset(reset), .out(out) );
endmodule
module sub_module; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    mods = scan_preprocessed(text, str(rtl))
    names = {e.inst_name for e in mods["middle_module"].instances}
    assert names == {"u_subTop_0", "u_sub_1"}


def test_ifdef_preprocessed_instances(tmp_path):
    rtl = tmp_path / "t.v"
    rtl.write_text(
        """
`ifdef USE_A
module top;
  a_mod u_a ();
endmodule
`else
module top;
  b_mod u_b ();
endmodule
`endif
module a_mod; endmodule
module b_mod; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {"USE_A": "1"})
    mods = scan_preprocessed(text, str(rtl))
    assert any(e.child_module == "a_mod" for e in mods["top"].instances)
    assert not any(e.child_module == "b_mod" for e in mods["top"].instances)