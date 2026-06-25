"""Verification step and item timing logs."""

from __future__ import annotations

import io
import subprocess
import textwrap
from pathlib import Path

from hierwalk.verification_timing import (
    VerificationTimingRecorder,
    record_connect_check,
    set_active_recorder,
    verification_step,
)


def test_recorder_emits_item_step_and_summary():
    rec = VerificationTimingRecorder(quiet=True)
    buf = io.StringIO()
    rec._streams = lambda: [buf]  # type: ignore[method-assign]

    with verification_step(kind="run_conn_check", name="run_conn_check[0]", recorder=rec):
        record_connect_check(
            check_id="a",
            endpoint_a="top.u_a.p",
            endpoint_b="top.u_b.p",
            elapsed_sec=0.12,
        )
        record_connect_check(
            check_id="b",
            endpoint_a="top.u_c.p",
            endpoint_b="top.u_d.p",
            elapsed_sec=0.08,
        )

    rec.emit_summary()
    text = buf.getvalue()
    assert "[hier-walk verify-timing]" in text
    assert "item top.u_a.p -> top.u_b.p" in text
    assert "item id=b top.u_c.p -> top.u_d.p" in text
    assert "step run_conn_check[0] kind=run_conn_check" in text
    assert "--- summary ---" in text
    assert "text-conn:" in text or "logical-conn:" in text
    assert "run_conn_check[0] kind=run_conn_check" in text
    assert len(rec.steps) == 1
    assert rec.steps[0].elapsed_sec >= 0.0
    assert len(rec.steps[0].items) == 2


def test_recorder_writes_to_log_path(tmp_path: Path):
    log_path = tmp_path / "run.log"
    log_path.write_text("header\n", encoding="utf-8")
    rec = VerificationTimingRecorder(quiet=True)

    with verification_step(
        kind="run_io_trace",
        name="run_io_trace[0]",
        recorder=rec,
        log_path=log_path,
    ):
        set_active_recorder(rec)
        from hierwalk.verification_timing import record_verification_item

        record_verification_item("top.u_inst", 0.5)
        set_active_recorder(None)

    rec.emit_summary()
    log_text = log_path.read_text(encoding="utf-8")
    assert "verify-timing" in log_text
    assert "top.u_inst" in log_text
    assert "--- summary ---" in log_text


def test_cli_check_connect_emits_verify_timing(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        textwrap.dedent(
            """
            module top(input logic clk);
              sub u0(.clk(clk));
            endmodule
            module sub(input logic clk);
            endmodule
            """
        ).strip(),
        encoding="utf-8",
    )
    fl = tmp_path / "files.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")
    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--no-cache",
            "--check-connect",
            "top.clk",
            "top.u0.clk",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    merged = proc.stderr + proc.stdout
    assert "[hier-walk verify-timing]" in merged
    assert "--- summary ---" in merged
    assert "item top.clk -> top.u0.clk" in merged