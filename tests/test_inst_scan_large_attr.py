"""Instance scan on large bodies with SV attributes before inst names."""

from __future__ import annotations

from hierwalk.inst_scan import _LARGE_BODY_ATTR_SKIP, scan_hierarchy_instances


def test_large_body_finds_inst_after_sv_attributes():
    padding = "// pad\n" * (_LARGE_BODY_ATTR_SKIP // 8 + 2000)
    body = (
        padding
        + """
module PARENT;
  CPUSYSTEM_TOP (* keep_hierarchy = "true" *) u_cpusystem_top (
    .clk(clk)
  );
endmodule
"""
    )
    edges = scan_hierarchy_instances(body)
    assert any(e.inst_name == "u_cpusystem_top" for e in edges)