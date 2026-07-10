"""hgconn bloom probes — no false negative on obvious connects."""

from __future__ import annotations

from hgconn.bloom import bloom_connect


def test_bloom_same_net_always_connected():
    body = "module m; wire x; assign x = y; endmodule"
    p = bloom_connect(body, name_a="x", name_b="x")
    assert p.connected
    assert p.mode == "same-net"


def test_bloom_assign_rhs_word_fallback():
    body = """
    module child (output logic out);
      assign out = 1'b0;
    endmodule
    """
    p = bloom_connect(body, name_a="out", name_b="out")
    assert p.connected


def test_bloom_both_names_present_lenient():
    body = "module m; wire a, b; assign a = b; endmodule"
    p = bloom_connect(body, name_a="a", name_b="b")
    assert p.connected
    assert p.mode in ("assign", "word")