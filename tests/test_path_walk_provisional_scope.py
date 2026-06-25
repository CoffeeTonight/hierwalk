"""Provisional pass-through for generate/array hierarchy segments."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def test_generate_block_segment_provisional_pass_through(tmp_path: Path):
    soc = tmp_path / "soc.v"
    soc.write_text(
        "module SOC_TOP;\n"
        "genvar gi;\n"
        "generate\n"
        "  for (gi = 0; gi < 2; gi++) begin : gen_blk\n"
        "    BCD u_BCD_gen (.clk(clk));\n"
        "  end\n"
        "endgenerate\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "bcd.v").write_text("module BCD; endmodule\n", encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(str(p.resolve()) for p in (soc, tmp_path / "bcd.v")) + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    target = "SOC_TOP.gen_blk[0].u_BCD_gen"
    batch, _index, state = run_path_walk_connect(
        ConnectivityRequest(checks=(ConnectivityCheck(target, target),), top="SOC_TOP"),
        flr,
        top="SOC_TOP",
        no_cache=True,
    )
    scope = state.rows_by_path.get("SOC_TOP.gen_blk[0]")
    assert scope is not None
    assert scope.refine_status == "provisional"
    assert "provisional" in scope.walk_note.lower()
    assert target in state.rows_by_path
    assert target in state.rows_by_path
    assert batch.results[0].connected_logical is False
    assert batch.results[0].connected is False