"""Connect trace log: timestamps and COI walk on failure."""

from __future__ import annotations

import io
import re
from pathlib import Path

from hierwalk.connectivity import ConnectivitySession, emit_connect_trace_log
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def _build_session(tmp_path: Path) -> ConnectivitySession:
    top = tmp_path / "top.v"
    top.write_text(
        """
        module top(input logic clk, input logic a, output logic b);
          wire n;
          assign n = clk;
          assign b = a;
        endmodule
        """,
        encoding="utf-8",
    )
    child = tmp_path / "child.v"
    child.write_text(
        """
        module child(input logic x, output logic y);
          assign y = x;
        endmodule
        """,
        encoding="utf-8",
    )
    index = DesignIndex.build(
        {
            str(top.resolve()): top.read_text(encoding="utf-8"),
            str(child.resolve()): child.read_text(encoding="utf-8"),
        }
    )
    _, rows = elaborate(index, "top")
    return ConnectivitySession(rows=rows, index=index, top="top")


def test_emit_connect_log_has_timestamp_and_walk_on_failure(tmp_path: Path):
    session = _build_session(tmp_path)
    result = session.check("top.clk", "top.b", trace=True, check_id="t1")
    assert not result.connected

    buf = io.StringIO()
    emit_connect_trace_log(
        result,
        stream=buf,
        check_prefix="t1",
        rows_by_path=session.rows_by_path,
    )
    text = buf.getvalue()
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
    assert "[hier-walk connect] [t1]" in text
    assert "not connected" in text
    assert "connect walk (COI search):" in text
    assert "path hierarchy (rtl + filelist):" in text
    assert "rtl=" in text
    assert result.coi_walk is not None
    assert result.walk_notes


def test_emit_connect_log_success_still_timestamped(tmp_path: Path):
    session = _build_session(tmp_path)
    result = session.check("top.clk", "top.n", trace=True, check_id="ok")
    assert result.connected

    buf = io.StringIO()
    emit_connect_trace_log(
        result,
        stream=buf,
        check_prefix="ok",
        rows_by_path=session.rows_by_path,
    )
    text = buf.getvalue()
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
    assert "connected:" in text
    assert "path evidence:" in text or "path hierarchy" in text