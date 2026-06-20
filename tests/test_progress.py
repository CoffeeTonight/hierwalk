"""Progress reporting during filelist expansion."""

from __future__ import annotations

import io

from hierwalk.filelist import parse_filelist
from hierwalk.progress import (
    ProgressHeartbeat,
    ProgressReporter,
    format_hierwalk_log,
    format_work_location,
    progress_callback,
    split_progress_detail,
)


def test_format_hierwalk_log_includes_timestamp():
    line = format_hierwalk_log("index: done")
    assert line.startswith("20")
    assert "[hier-walk] index: done" in line


def test_progress_reporter_phase_includes_timestamp():
    buf = io.StringIO()
    reporter = ProgressReporter(stream=buf, enabled=True)
    reporter.phase("cache: hit")
    text = buf.getvalue()
    assert text.startswith("20")
    assert "[hier-walk] cache: hit" in text


def test_format_work_location_includes_listing_filelist():
    via = {"/eda/soc/rtl/cpu/alu/foo.v": "/eda/soc/lists/cpu_block.f"}
    loc = format_work_location(
        "/eda/soc/rtl/cpu/alu/foo.v",
        index=3,
        total=100,
        via_map=via,
    )
    assert "listing: cpu_block.f" in loc
    assert "file: foo.v" in loc


def test_format_work_location_shortens_long_paths():
    loc = format_work_location(
        "/eda/soc/rtl/cpu/alu/foo.v",
        index=12,
        total=12000,
    )
    assert "folder: cpu/alu" in loc
    assert "file: foo.v" in loc
    assert "(12/12000)" in loc


def test_progress_reporter_detail_for_heartbeat():
    reporter = ProgressReporter(stream=io.StringIO(), enabled=True)
    reporter.set_filelist("/proj/design/filelist.f")
    reporter.absorb_progress(
        "index: scanning 500/12000 files — "
        "listing: cpu_block.f | folder: rtl/cpu | file: alu.v (500/12000)"
    )
    assert reporter.get_detail() == (
        "filelist: cpu_block.f | folder: rtl/cpu | file: alu.v (500/12000)"
    )


def test_track_work_updates_heartbeat_between_milestones():
    reporter = ProgressReporter(stream=io.StringIO(), enabled=True)
    reporter.set_filelist("top.f")
    sink = progress_callback(reporter)
    assert sink is not None
    via = {
        "/eda/soc/rtl/cpu/alu/foo.v": "/eda/soc/lists/cpu_block.f",
        "/eda/soc/rtl/dv/tb_top.v": "/eda/soc/lists/dv.f",
    }
    sink.track(
        "/eda/soc/rtl/cpu/alu/foo.v",
        index=501,
        total=12000,
        via_map=via,
    )
    assert "file: foo.v" in reporter.get_detail()
    assert "filelist: cpu_block.f" in reporter.get_detail()
    assert "(501/12000)" in reporter.get_detail()

    sink.track(
        "/eda/soc/rtl/dv/tb_top.v",
        index=750,
        total=12000,
        via_map=via,
    )
    detail = reporter.get_detail()
    assert "file: tb_top.v" in detail
    assert "filelist: dv.f" in detail
    assert "(750/12000)" in detail
    assert "foo.v" not in detail
    assert "cpu_block.f" not in detail


def test_progress_heartbeat_includes_detail():
    buf = io.StringIO()
    reporter = ProgressReporter(stream=buf, enabled=True)
    reporter.set_filelist("top.f")
    reporter.set_location("folder: dv | file: tb.v (1/100)")
    hb = ProgressHeartbeat(
        reporter.phase,
        "index",
        interval_sec=0.01,
        enabled=True,
        get_detail=reporter.get_detail,
    )
    with hb:
        import time

        time.sleep(0.05)
    text = buf.getvalue()
    assert "still running" in text
    assert "filelist: top.f" in text
    assert "folder: dv" in text


def test_split_progress_detail():
    assert split_progress_detail("index: 1/2 — folder: a | file: b.v") == (
        "folder: a | file: b.v"
    )
    assert split_progress_detail("cache: hit") is None


def test_filelist_progress_messages(tmp_path):
    nested = tmp_path / "nested.f"
    nested.write_text("child.v\n", encoding="utf-8")
    child = tmp_path / "child.v"
    child.write_text("module child; endmodule\n", encoding="utf-8")
    top = tmp_path / "top.f"
    top.write_text(f"-f {nested.name}\n", encoding="utf-8")

    lines: list[str] = []
    fl = parse_filelist(top, on_progress=lines.append)

    assert len(fl.source_files) == 1
    assert any("expanding" in line for line in lines)
    assert any("reading top.f" in line for line in lines)
    assert any("reading nested.f" in line for line in lines)
    assert any("done —" in line for line in lines)