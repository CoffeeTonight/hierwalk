"""Instances under ``ifndef`` with macro cell types."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.index import scan_preprocessed
from hierwalk.preprocess import clear_include_unit_cache, preprocess_file_for_index


def test_ifndef_macro_cell_preprocessed_for_index(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
`define CELL leaf
module top;
`ifndef NO_INST
  `CELL u_x ();
`endif
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    text = preprocess_file_for_index(rtl, [tmp_path], {})
    assert "leaf u_x" in text
    assert "`CELL" not in text


def test_ifndef_macro_cell_index_and_cli(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
`define CELL leaf
module top;
`ifndef NO_INST
  `CELL u_x ();
`endif
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")

    clear_include_unit_cache()
    text = preprocess_file_for_index(rtl, [tmp_path], {})
    mods = scan_preprocessed(text, str(rtl))
    assert any(
        e.inst_name == "u_x" and e.child_module == "leaf"
        for e in mods["top"].instances
    )

    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "top",
                "defines": {},
                "no_cache": True,
            }
        ),
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
    assert "top.u_x" in paths