"""Text-conn COI dedup: coarse slice keys, logical conn unaffected."""

from __future__ import annotations

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connectivity import ConnectivitySession
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


def test_text_coi_dedup_batch_rejects_unwired_slice(tmp_path):
    """Batch COI dedup must not fan out a wired slice verdict onto unwired bits."""
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
    assert by_id["unwired"] is False
    assert batch.text_coi_unique == 2


def test_text_coi_dedup_bloom_rejects_unwired_slice(tmp_path):
    """Text-conn: literal slice-only assigns must not bloom-unwire other bits."""
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
    wired = session.check("top.data[0]", "top.out[0]")
    assert wired.connected
    unwired = session.check("top.data[3]", "top.out[3]")
    assert not unwired.connected


def test_fanout_resolves_shared_endpoint_once(tmp_path):
    from unittest.mock import patch

    from hierwalk.connect_endpoints import resolve_endpoint
    from hierwalk.connect_request import parse_connect_request_json
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
        "hierwalk.connectivity.resolve_endpoint",
        wraps=resolve_endpoint,
    ) as mock_resolve:
        batch = session.run_text_request(request)

    assert batch.results[0].connected is False
    specs = [call.args[0] for call in mock_resolve.call_args_list]
    assert specs.count("top.wq") == 1
    assert specs.count("top.wa") == 1
    assert specs.count("top.wc") == 1
    assert specs.count("top.wb") == 1