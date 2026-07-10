"""Hierarchy resolve: `` `ifndef not_A `` + ``A`` + `` `ifndef _B `` + cell-less ``u_A``."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hierwalk.hierarchy_grep import HierarchyGrepSession, _inst_child_module
from hierwalk.inst_scan import find_hierarchy_instance
from hierwalk.preprocess import apply_ifdef_filter, strip_comments_for_instance_scan

# User RTL (``\nA`` in messages = newline + cell type ``A``, NOT macro ``not_A\A``):
#
#   // blabla
#   `ifndef not_A
#   A
#   `ifndef _B
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
    `ifndef _B
    #(.P(1))
    `endif
    u_A ();
    `endif
    endmodule
    """


def test_ifndef_filters_leave_A_then_u_A():
    filt = _filtered(_user_top_body())
    assert "ifndef" not in filt
    assert "A\n" in filt or filt.startswith("module top;\nA\n")
    assert "u_A" in filt


def test_A_on_own_line_between_ifndefs_pairs_with_u_A():
    body = _user_top_body() + "\nmodule A; endmodule\n"
    filt = _filtered(body)
    edge = find_hierarchy_instance(filt, "u_A")
    assert edge is not None
    assert edge.inst_name == "u_A"
    assert edge.child_module == "A"
    assert edge.param_overrides.get("P") == "1"
    assert _inst_child_module(body, "u_A") == "A"


def test_resolve_top_u_a_out_user_ifndef_pattern():
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