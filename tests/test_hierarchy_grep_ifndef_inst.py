"""Hierarchy: `` `ifndef not_A `` + ``A`` + `` `ifndef B `` + ``u_A``."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hierwalk.hierarchy_grep import HierarchyGrepSession, _inst_child_module
from hierwalk.inst_scan import find_hierarchy_instance
from hierwalk.preprocess import apply_ifdef_filter, strip_comments_for_instance_scan

# Exact user RTL (A on its own line BETWEEN the two ifndefs — NOT `` `ifndef not_A\\A ``):
#
#   // blabla
#   `ifndef not_A
#   A
#   `ifndef B
#   #(...)
#   `endif
#   u_A (...);


def _filtered(body: str) -> str:
    return apply_ifdef_filter(strip_comments_for_instance_scan(body), {})


def _user_top_body() -> str:
    return """
    module top;
    // blabla
    `ifndef not_A
    A
    `ifndef B
    #(.P(1))
    `endif
    u_A ();
    `endif
    endmodule
    """


def test_filtered_body_has_A_then_u_A():
    filt = _filtered(_user_top_body())
    assert "ifndef" not in filt
    assert "A\n" in filt or " A" in filt
    assert "u_A" in filt


def test_ifdef_positive_wrapper_finds_u_A():
    """Production `` `ifdef CHIP_HAS_A`` (not `` `ifndef not_A``) must resolve."""
    body = """
    module A (output logic out);
      assign out = 1'b0;
    endmodule
    module top;
    `ifdef CHIP_HAS_A
    A
    `ifndef B
    #(.P(1))
    `endif
    u_A ();
    `endif
    endmodule
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "top.v"
        path.write_text(body, encoding="utf-8")
        session = HierarchyGrepSession.from_rtl_paths(
            [str(path)],
            build_file_index_background=False,
        )
        result = session.resolve("top.u_A.out", top="top")
        assert result.get("ok") is True, result.get("error")


def test_A_line_between_ifndefs_pairs_with_u_A():
    body = _user_top_body() + "\nmodule A; endmodule\n"
    filt = _filtered(body)
    edge = find_hierarchy_instance(filt, "u_A")
    assert edge is not None
    assert edge.inst_name == "u_A"
    assert edge.child_module == "A"
    assert _inst_child_module(body, "u_A") == "A"


def test_ifndef_with_trailing_line_comments():
    body = """
    module top;
    `ifndef not_A  // guard
    A  // cell
    `ifndef B  // inner
    #(.P(1))  // params
    `endif  // end B block
    u_A ();  // inst
    `endif  // end not_A
    endmodule
    module A (output logic out); assign out=0; endmodule
    """
    filt = _filtered(body)
    assert "B\n" not in filt and "not_A\n" not in filt
    assert _inst_child_module(body, "u_A") == "A"
    edge = find_hierarchy_instance(filt, "u_A")
    assert edge is not None and edge.child_module == "A"


def test_escaped_module_index_and_port():
    rtl = """
    module \\BUF_ESC (output logic o);
      assign o = 1'b1;
    endmodule
    module top;
    `ifndef not_ESC
    \\BUF_ESC
    u_esc ();
    `endif
    endmodule
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "top.v"
        path.write_text(rtl, encoding="utf-8")
        session = HierarchyGrepSession.from_rtl_paths(
            [str(path)],
            build_file_index_background=False,
        )
        result = session.resolve("top.u_esc.o", top="top")
        assert result.get("ok") is True, result.get("error")


def test_resolve_top_u_a_out():
    rtl = """
    module A (output logic out);
      assign out = 1'b0;
    endmodule
    """ + _user_top_body()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "top.v"
        path.write_text(rtl, encoding="utf-8")
        session = HierarchyGrepSession.from_rtl_paths(
            [str(path)],
            build_file_index_background=False,
        )
        result = session.resolve("top.u_A.out", top="top")
        assert result.get("ok") is True, result.get("error")