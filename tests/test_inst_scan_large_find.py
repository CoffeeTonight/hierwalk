"""Selective instance lookup on monolithic allinst-scale bodies."""

from __future__ import annotations

import time

from hierwalk.inst_scan import _LARGE_BODY_SLIM, find_hierarchy_instance


def test_find_hierarchy_instance_anchors_tail_of_large_body():
    stub = "  STUB_{i} u_stub_{i} ( );\n"
    n = (_LARGE_BODY_SLIM // len(stub)) + 5000
    body = "".join(stub.format(i=i) for i in range(n))
    body += "  TARGET_MOD u_target ( );\n"
    assert len(body) > _LARGE_BODY_SLIM

    t0 = time.perf_counter()
    edge = find_hierarchy_instance(body, "u_target")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert edge is not None
    assert edge.child_module == "TARGET_MOD"
    assert edge.inst_name == "u_target"
    assert elapsed_ms < 2000.0


def test_find_hierarchy_instance_anchors_multiline_param_inst():
    pad = "  PAD_{i} u_pad_{i} ( );\n"
    n = (_LARGE_BODY_SLIM // len(pad)) + 2000
    body = "".join(pad.format(i=i) for i in range(n))
    body += (
        "  CHILD #(\n"
        "    .W(8)\n"
        "  ) u_child (\n"
        "    .clk(clk)\n"
        "  );\n"
    )
    edge = find_hierarchy_instance(body, "u_child")
    assert edge is not None
    assert edge.child_module == "CHILD"