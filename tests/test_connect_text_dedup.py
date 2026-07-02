"""Text-conn grep dedup: coarse base keys, logical conn unaffected."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import ConnectivitySession
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _bus_top_v() -> str:
    return """
    module top #(
      parameter int W = 4
    )(
      input logic clk,
      input logic [W-1:0] data,
      output logic [W-1:0] out0,
      output logic [W-1:0] out1,
      output logic [W-1:0] out2,
      output logic [W-1:0] out3
    );
      wire [W-1:0] n;
      assign n = data;
      assign out0 = n;
      assign out1 = n;
      assign out2 = n;
      assign out3 = n;
    endmodule
    """


def _session(tmp_path) -> ConnectivitySession:
    rtl = tmp_path / "top.v"
    rtl.write_text(_bus_top_v(), encoding="utf-8")
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    return ConnectivitySession(rows=rows, index=index, top="top", resolve_param_dims=False)


def test_text_coi_dedup_collapses_slice_matrix(tmp_path):
    session = _session(tmp_path)
    checks = tuple(
        ConnectivityCheck(f"top.data[{i}]", f"top.out0[{i}]", check_id=f"c{i}")
        for i in range(4)
    )
    request = ConnectivityRequest(checks=checks, top="top")

    batch = session.run_text_request(request)
    assert batch.text_coi_leaves == 4
    assert batch.text_coi_unique == 1
    assert len(batch.results) == 4
    for result in batch.results:
        assert result.connected
        assert result.endpoint_a.spec.startswith("top.data[")
        assert result.endpoint_b.spec.startswith("top.out0[")


def test_text_coi_dedup_keeps_distinct_b_targets(tmp_path):
    session = _session(tmp_path)
    checks = (
        ConnectivityCheck("top.data[0]", "top.out0[0]", check_id="a0"),
        ConnectivityCheck("top.data[1]", "top.out1[1]", check_id="a1"),
    )
    request = ConnectivityRequest(checks=checks, top="top")
    batch = session.run_text_request(request)
    assert batch.text_coi_leaves == 2
    assert batch.text_coi_unique == 2
    assert all(r.connected for r in batch.results)


def test_logical_run_request_does_not_dedup_slices(tmp_path):
    session = _session(tmp_path)
    session.resolve_param_dims = True
    checks = tuple(
        ConnectivityCheck(f"top.data[{i}]", f"top.out{i}[{i}]", check_id=f"c{i}")
        for i in range(4)
    )
    request = ConnectivityRequest(checks=checks, top="top")
    batch = session.run_request(request, jobs=1)
    assert batch.text_coi_leaves == 0
    assert batch.text_coi_unique == 0
    assert len(batch.results) == 4
    assert all(r.connected for r in batch.results)


def test_text_coi_dedup_batch_coarse_blooms_unwired_slice(tmp_path):
    """Text grep: base-level bloom may pass unwired slice bits (logical refines)."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top #(
          parameter int W = 4
        )(
          input logic [W-1:0] data,
          output logic [W-1:0] out
        );
          assign out[0] = data[0];
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.data[0]", "top.out[0]", check_id="wired"),
            ConnectivityCheck("top.data[3]", "top.out[3]", check_id="unwired"),
        ),
        top="top",
    )
    batch = session.run_text_request(request)
    by_id = {r.check_id: r.connected for r in batch.results}
    assert by_id["wired"] is True
    assert by_id["unwired"] is True
    assert batch.text_coi_unique == 1


def test_text_coi_dedup_bloom_coarse_passes_unwired_slice(tmp_path):
    """Text grep: coarse base bloom may pass unwired slice bits."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top #(
          parameter int W = 4
        )(
          input logic [W-1:0] data,
          output logic [W-1:0] out
        );
          assign out[0] = data[0];
        endmodule
        """,
        encoding="utf-8",
    )
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    wired = session.check_text("top.data[0]", "top.out[0]")
    assert wired.connected
    unwired = session.check_text("top.data[3]", "top.out[3]")
    assert unwired.connected

    session.resolve_param_dims = True
    logical = session.check("top.data[3]", "top.out[3]")
    assert not logical.connected


def test_text_grep_passes_zero_mult_logical_disconnects(tmp_path):
    """``assign a = b * 0``: text sees RHS name; logical masks non-propagating drive."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top;
          wire a, b;
          assign a = b * 0;
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    text = ConnectivitySession(
        rows=rows, index=index, top="top", resolve_param_dims=False
    )
    assert text.check_text("top.b", "top.a").connected is True

    logical = ConnectivitySession(
        rows=rows, index=index, top="top", resolve_param_dims=True
    )
    assert logical.check("top.b", "top.a").connected is False


def test_text_grep_passes_masked_and_logical_disconnects(tmp_path):
    """``assign dst = src & 1'b0``: text grep passes; logical tie-off masks."""
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top(input src, output dst);
          assign dst = src & 1'b0;
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    text = ConnectivitySession(
        rows=rows, index=index, top="top", resolve_param_dims=False
    )
    assert text.check_text("top.src", "top.dst").connected is True

    logical = ConnectivitySession(
        rows=rows, index=index, top="top", resolve_param_dims=True
    )
    assert logical.check("top.src", "top.dst").connected is False


def test_fanout_resolves_shared_endpoint_once(tmp_path):
    from unittest.mock import patch

    from hierwalk.connect.shared.endpoints import resolve_endpoint
    from hierwalk.connect.shared.request import parse_connect_request_json
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "  wire wa, wb, wc, wq;\n"
        "  assign wq = wb;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    index = DesignIndex.build({str(rtl.resolve()): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    request = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ]
        }
    )

    with patch(
        "hierwalk.connect.shared.resolve_cache.resolve_endpoint",
        wraps=resolve_endpoint,
    ) as mock_resolve:
        batch = session.run_text_request(request)

    assert batch.results[0].connected is False
    specs = [call.args[0] for call in mock_resolve.call_args_list]
    assert specs.count("top.wq") == 1
    assert specs.count("top.wa") == 1
    assert specs.count("top.wc") == 1
    assert specs.count("top.wb") == 1


def test_text_conn_mixed_slice_blooms_to_base(tmp_path: Path):
    """Base net and one slice share net_rep; text-conn treats them as equivalent."""
    v = """
    module top;
      wire [1:0] bus;
      wire out;
      assign out = bus[0];
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check_text("top.bus", "top.out").connected is True
    assert session.check_text("top.bus[0]", "top.out").connected is True


def test_text_walk_session_cache_survives_across_checks(tmp_path):
    """Module/net rep memo from one check is reused on the next (same session)."""
    v = """
    module top;
      wire [3:0] a, b;
      assign b = a;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    session.check_text("top.a[0]", "top.b[0]")
    wc = session.text_walk_caches
    assert len(wc.scope_mod_idx) >= 1
    assert len(wc.net_rep_cache) >= 1
    before_equiv = len(wc.equiv_cache)
    before_parent = len(wc.parent_up_cache)
    session.check_text("top.a[1]", "top.b[1]")
    assert "top" in wc.scope_mod_idx
    assert len(wc.net_rep_cache) >= 2
    assert len(wc.equiv_cache) >= before_equiv
    assert len(wc.parent_up_cache) >= before_parent


def test_text_walk_profiling_counters_increment(tmp_path):
    """Text-conn records grep-cache miss profile; expand counts when BFS expands."""
    v = """
    module top;
      wire [1:0] a, b;
      assign b = a;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check_text("top.a[0]", "top.b[1]").connected is True
    wc = session.text_walk_caches
    assert wc.grep_cache_miss >= 1
    assert wc.rep_adj_capped >= 0


def test_text_walk_verdict_cache_reuses_bfs(tmp_path):
    """Rep-level walk verdict cache skips repeat BFS when trace=False."""
    v = """
    module top;
      wire a, b, c;
      assign b = a;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check_text("top.a", "top.c").connected is False
    before_expand = session.text_walk_caches.expand_calls
    assert len(session.text_walk_caches.walk_verdict_cache) >= 1
    assert session.check_text("top.a", "top.c").connected is False
    wc = session.text_walk_caches
    assert wc.walk_verdict_hits >= 1
    assert wc.expand_calls == before_expand


def test_slice_meets_base_via_rep_index(tmp_path):
    """Slice vs base endpoint still connects (rep-index meet, not slice-slice)."""
    v = """
    module top;
      wire [3:0] bus;
      wire out;
      assign out = bus;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check_text("top.bus[2]", "top.out").connected is True
    assert session.check_text("top.bus", "top.out").connected is True