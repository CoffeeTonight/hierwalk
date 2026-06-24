"""Fast statement splitting for assign-heavy module bodies."""

from __future__ import annotations

import time
from pathlib import Path

from hierwalk.connect_scan import split_statements


def test_split_statements_line_fast_on_large_assign_block():
    n = 60_000
    body = "module MOD_A(input logic clk);\n"
    body += "".join(f"  assign w{i} = clk;\n" for i in range(n))
    body += "  MOD_B b (.clk(clk));\nendmodule\n"

    t0 = time.perf_counter()
    stmts = split_statements(body)
    first_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    stmts2 = split_statements(body)
    cached_ms = (time.perf_counter() - t0) * 1000.0

    assert len(stmts) == n + 3  # module header + assigns + instance + endmodule
    assert stmts == stmts2
    assert first_ms < 5_000.0
    assert cached_ms < 50.0


def test_split_statements_slow_path_preserves_begin_block():
    body = """
    module m;
      always_comb begin
        a = b;
        c = d;
      end
    endmodule
    """
    stmts = split_statements(body)
    joined = " ".join(stmts)
    assert "always_comb begin" in joined
    assert "a = b" in joined