"""hg_core log/report smoke tests."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

from hg_core.log import emit_hg_log
from hg_core.report import ReportBuilder, format_elapsed_sec


def test_emit_hg_log_has_timestamp():
    buf = io.StringIO()
    emit_hg_log("flat-db hit", tool="hgpath", stream=buf)
    text = buf.getvalue()
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[hgpath\] flat-db hit", text)


def test_report_includes_elapsed(tmp_path: Path):
    t0 = time.perf_counter()
    rb = ReportBuilder(title="hgpath report", tool="hgpath", started_at=t0)
    rb.add("modules: 2")
    out = tmp_path / "hgpath.report"
    text = rb.finish(out)
    assert "elapsed:" in text
    assert "modules: 2" in text
    assert out.is_file()


def test_format_elapsed_ms():
    t0 = time.perf_counter()
    assert format_elapsed_sec(t0, end=t0 + 0.05).endswith("ms")