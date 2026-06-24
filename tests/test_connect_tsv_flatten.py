"""TSV / trace output flattens expanded connect checks to per-bit rows."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hierwalk.connect_request import parse_connect_request_json
from hierwalk.connectivity import (
    flatten_connect_results,
    format_connect_results_tsv,
    run_connectivity_request,
)
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _elab(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows, top


def _tsv_rows(tsv: str) -> list[dict[str, str]]:
    lines = [ln for ln in tsv.strip().splitlines() if ln and not ln.startswith("#")]
    headers = lines[0].split("\t")
    return [dict(zip(headers, row.split("\t"))) for row in lines[1:]]


def test_flatten_array_bus_emits_per_bit_rows(tmp_path: Path):
    verilog = """
    module top(input logic clk);
      wire a0, a1;
      wire [1:0] bus_b;
      assign a0 = clk;
      assign a1 = clk;
      assign bus_b[0] = a0;
      assign bus_b[1] = a1;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "bus",
                    "a": ["top.a0", "top.a1"],
                    "b": "top.bus_b[1:0]",
                }
            ]
        }
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    parent = batch.results[0]
    assert len(parent.sub_results) == 2

    leaves = flatten_connect_results(batch.results)
    assert len(leaves) == 2
    assert {r.check_id for r in leaves} == {"bus[0]", "bus[1]"}

    rows_out = _tsv_rows(format_connect_results_tsv(batch.results))
    assert len(rows_out) == 2
    assert {r["check_id"] for r in rows_out} == {"bus[0]", "bus[1]"}
    assert rows_out[0]["endpoint_a"] == "top.a0"
    assert rows_out[0]["endpoint_b"] == "top.bus_b[0]"
    assert rows_out[1]["endpoint_a"] == "top.a1"
    assert rows_out[1]["endpoint_b"] == "top.bus_b[1]"
    assert all(r["connected"] == "True" for r in rows_out)


def test_flatten_fanout_scalar_sink_rows(tmp_path: Path):
    verilog = """
    module top(input logic src);
      wire d0, d1;
      assign d0 = src;
      assign d1 = src;
    endmodule
    """
    index, rows, top = _elab(verilog, tmp_path)
    req = parse_connect_request_json(
        {"checks": [{"id": "fan", "a": "top.src", "b": ["top.d0", "top.d1"]}]}
    )
    batch = run_connectivity_request(req, rows=rows, index=index, top=top)
    rows_out = _tsv_rows(format_connect_results_tsv(batch.results))
    assert len(rows_out) == 2
    assert rows_out[0]["endpoint_a"] == "top.src"
    assert rows_out[1]["endpoint_a"] == "top.src"
    assert {r["endpoint_b"] for r in rows_out} == {"top.d0", "top.d1"}


def test_hierwalk_cli_flattened_array_tsv(tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top(input logic clk);
  wire a0, a1;
  wire [1:0] bus_b;
  assign a0 = clk;
  assign a1 = clk;
  assign bus_b[0] = a0;
  assign bus_b[1] = a1;
endmodule
""",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")
    out = tmp_path / "conn.tsv"
    batch_json = tmp_path / "checks.json"
    batch_json.write_text(
        """
{
  "checks": [
    {"id": "bus", "a": ["top.a0", "top.a1"], "b": "top.bus_b[1:0]"}
  ]
}
""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--mode",
            "check-connect-batch",
            "--check-connect-batch",
            str(batch_json),
            "--no-cache",
            "--quiet",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    rows_out = _tsv_rows(out.read_text(encoding="utf-8"))
    assert len(rows_out) == 2
    assert rows_out[0]["check_id"] == "bus[0]"
    assert rows_out[1]["check_id"] == "bus[1]"