"""Nested ``ifndef`` with plain cell instances (DEF u_DEF)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.index import scan_preprocessed
from hierwalk.preprocess import clear_include_unit_cache, preprocess_file_for_index


def test_nested_ifndef_inside_port_map_index(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
module top;
`ifndef ABC
 DEF u_DEF (
`ifndef PORT_X
   .QW (w_QW),
`endif
`ifndef PORT_Y
   .CLK (clk)
`endif
 );
`endif
endmodule
module DEF(input CLK, output QW); endmodule
""",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file_for_index(rtl, [tmp_path], {})
    mods = scan_preprocessed(text, str(rtl))
    assert any(e.inst_name == "u_DEF" and e.child_module == "DEF" for e in mods["top"].instances)


def test_nested_ifndef_cli(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
module top;
`ifndef ABC
 DEF u_DEF (
`ifndef PORT_X
   .QW (w_QW),
`endif
   .CLK (clk)
 );
`endif
endmodule
module DEF(input CLK, output QW); endmodule
""",
        encoding="utf-8",
    )
    (tmp_path / "design.f").write_text(f"{rtl}\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps({"filelist": "design.f", "top": "top", "no_cache": True}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=True,
    )
    paths = [ln.split("\t", 1)[0] for ln in proc.stdout.splitlines()[1:] if ln.strip()]
    assert "top.u_DEF" in paths