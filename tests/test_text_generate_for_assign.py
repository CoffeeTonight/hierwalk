"""Text-COI must see assigns inside constant generate-for (genvar) loops."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.request import parse_connect_request_json
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def _write(tmp: Path, name: str, text: str) -> None:
    (tmp / name).write_text(text, encoding="utf-8")


def test_text_coi_through_generate_for_assign(tmp_path: Path):
    """
    stress_spine_3-style passthrough: probe_in → link only inside
    ``for (genvar gi = 0; gi < 1; gi++)``. Without generate-fold on the text
    index, COI stops at probe_in.
    """
    _write(
        tmp_path,
        "top.sv",
        """
        module mid (
          input  logic probe_in,
          output logic probe_out
        );
          wire link;
          generate
            for (genvar gi = 0; gi < 1; gi++) begin : gen_pass
              assign link = probe_in;
            end
          endgenerate
          assign probe_out = link;
        endmodule
        module top;
          logic a, b;
          mid u0 (.probe_in(a), .probe_out(b));
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "include_ff": True,
            "checks": [{"id": "thru_gen", "a": "top.a", "b": "top.b"}],
        }
    )
    batch, _, _ = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase="text",
        connect_output_dir=tmp_path / "db",
        no_cache=True,
    )
    assert len(batch.results) == 1
    r = batch.results[0]
    assert r.connected, f"expected connected through generate-for; note={r.note!r}"


def test_stress_alt_omitted_from_default_defines():
    """``STRESS_ALT: \"0\"`` still defines the macro and breaks `` `ifdef ``."""
    from hierwalk.stress_gen import STANDARD_CONFIG, generate_stress_design

    d = generate_stress_design(
        seed=42, depth=4, branch_factor=2, config=STANDARD_CONFIG
    )
    assert "STRESS_USE_IN" in d.defines
    assert "STRESS_ALT" not in d.defines
