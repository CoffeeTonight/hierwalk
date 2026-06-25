"""Text-conn inst grep with ``[]`` stripped to base identifier."""

from __future__ import annotations

from hierwalk.inst_scan import (
    _LARGE_BODY_SLIM,
    find_hierarchy_instance,
    inst_base_name,
    probe_inst_in_module_text,
)


def test_inst_base_name_strips_dimensions():
    assert inst_base_name("c[0][1]") == "c"
    assert inst_base_name("gen_blk[2]") == "gen_blk"
    assert inst_base_name("u") == "u"
    assert inst_base_name("\\top.u ") == "\\top.u"


def test_find_hierarchy_instance_base_fallback_wrong_index():
    body = "  child c [3:0] ( );\n"
    assert find_hierarchy_instance(body, "c[2]") is not None
    assert find_hierarchy_instance(body, "c[99]") is not None


def test_find_hierarchy_instance_base_fallback_param_range():
    body = "parameter N = 2;\n  mem u_b [N:0] ( );\n"
    assert find_hierarchy_instance(body, "u_b[1]", param_map={"N": "2"}) is not None
    assert find_hierarchy_instance(body, "u_b[9]", param_map={}) is not None


def test_find_hierarchy_instance_base_fallback_large_body():
    stub = "  STUB_{i} u_stub_{i} ( );\n"
    n = (_LARGE_BODY_SLIM // len(stub)) + 2000
    body = "".join(stub.format(i=i) for i in range(n))
    body += "  child arr [7:0] ( );\n"
    edge = find_hierarchy_instance(body, "arr[99]")
    assert edge is not None
    assert inst_base_name(edge.inst_name) == "arr"


def test_probe_inst_in_module_text_uses_base():
    body = "module m; child u [1:0] ( ); endmodule\n"
    assert probe_inst_in_module_text(body, "u[5]")
    assert not probe_inst_in_module_text(body, "missing[0]")