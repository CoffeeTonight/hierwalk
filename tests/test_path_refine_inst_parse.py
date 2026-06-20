"""path_refine instance parsing parity with inst_scan."""

from __future__ import annotations

from pathlib import Path

from hierwalk.index import DesignIndex
from hierwalk.path_refine import _body_prefix_before_instance, refine_param_ctx_for_path
from hierwalk.preprocess import preprocess_file


def test_body_prefix_matches_array_inst_name(tmp_path: Path):
    body = """
      localparam W_EARLY = 4;
      child #( .W(W_EARLY) ) u_arr[0] ();
      localparam W_LATE = 32;
    """
    prefix, _ = _body_prefix_before_instance(body, "u_arr[0]")
    assert "W_EARLY" in prefix
    assert "W_LATE" not in prefix


def test_body_prefix_matches_hierarchical_inst_leaf(tmp_path: Path):
    body = """
      localparam W_EARLY = 4;
      child genblk.u_target ();
      localparam W_LATE = 32;
    """
    prefix, _ = _body_prefix_before_instance(body, "u_target")
    assert "W_EARLY" in prefix
    assert "W_LATE" not in prefix


def test_body_prefix_consume_hash_parity_with_inst_scan():
    """Invalid ``#`` (no ``(#...)``) must rewind to ``start``, not advance to ``pos``."""
    body = """
      localparam W_BEFORE = 1;
      child # u_target ();
      localparam W_AFTER = 2;
    """
    prefix, _ = _body_prefix_before_instance(body, "u_target")
    assert "W_AFTER" in prefix


def test_body_prefix_skips_ifdef_directive_lines_before_target():
    """Parity with :func:`scan_hierarchy_instances` (directive lines stripped)."""
    body = """
      localparam W_BEFORE = 1;
      ////////
      `ifdef NO_A
      A u_a
      (
      .aa (w_aa));
      `endif
      `ifndef NO_CPU
      localparam W_CPU = 8;
      CPUSYSTEM_TOP u_cpusystem_top
      (
      .clk(clk));
      `endif
      localparam W_AFTER = 9;
    """
    prefix, params = _body_prefix_before_instance(body, "u_cpusystem_top")
    assert "W_BEFORE" in prefix
    assert "W_CPU" in params
    assert "W_AFTER" not in params
    assert "ifdef" not in prefix
    assert "NO_A" not in prefix


def test_body_prefix_skips_comma_separated_inst_before_target():
    body = """
      localparam W_EARLY = 4;
      child u_first (), u_target ();
      localparam W_LATE = 32;
    """
    prefix, _ = _body_prefix_before_instance(body, "u_target")
    assert "W_EARLY" in prefix
    assert "W_LATE" not in prefix


def test_find_child_instance_matches_array_base_name(tmp_path: Path):
    from hierwalk.path_refine import find_child_instance

    rtl = tmp_path / "arr_base.v"
    rtl.write_text(
        """
        module top;
          core u_core[3:0] ();
        endmodule
        module core;
        endmodule
        """,
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    edge = find_child_instance(index, "top", "u_core", {})
    assert edge is not None
    assert edge.child_module == "core"
    assert edge.inst_name.startswith("u_core[")


def test_find_child_instance_falls_back_to_prescanned_instances(tmp_path: Path):
    from hierwalk.path_refine import find_child_instance

    rtl = tmp_path / "stop_parent.v"
    rtl.write_text(
        """
        module top;
          child u0 ();
        endmodule
        module child;
        endmodule
        """,
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    rec = index.get_module("top")
    assert rec is not None and rec.instances
    rec.stop_reason = "ignorePath"
    rec.body = ""

    edge = find_child_instance(index, "top", "u0", {})
    assert edge is not None
    assert edge.inst_name == "u0"
    assert edge.child_module == "child"


def test_find_child_instance_honors_empty_scoped_params(tmp_path: Path):
    from hierwalk.path_refine import find_child_instance

    rtl = tmp_path / "empty_scope.v"
    rtl.write_text(
        """
        module top #(
            parameter int P = 99
        );
          child u0 ();
        endmodule
        module child;
        endmodule
        """,
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    edge = find_child_instance(index, "top", "u0", {}, scoped_params={})
    assert edge is not None
    assert edge.inst_name == "u0"


def test_refine_param_ctx_for_array_inst(tmp_path: Path):
    rtl = tmp_path / "arr.v"
    rtl.write_text(
        """
        module top;
          localparam W_EARLY = 8;
          child #( .W(W_EARLY) ) u_arr[0] ();
          localparam W_LATE = 64;
        endmodule
        module child #(parameter int W = 1) (
            input logic [W-1:0] data
        );
        endmodule
        """,
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    result = refine_param_ctx_for_path(index, "top", "top.u_arr[0]")
    assert result.ok
    assert result.param_ctx.get("W") == "8"