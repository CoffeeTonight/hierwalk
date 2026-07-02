"""Tests for preprocessing stderr tags (HIERWALK_PP_LOG)."""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr

from hierwalk.preprocess_log import PP_DISK, PP_MEM, PP_MISS, emit_pp_log


def test_emit_pp_log_off(monkeypatch):
    monkeypatch.setenv("HIERWALK_PP_LOG", "0")
    buf = io.StringIO()
    with redirect_stderr(buf):
        emit_pp_log(PP_MISS, "/rtl/foo.v", ms=5000.0)
    assert buf.getvalue() == ""


def test_emit_pp_log_brief_miss(monkeypatch):
    monkeypatch.setenv("HIERWALK_PP_LOG", "1")
    buf = io.StringIO()
    with redirect_stderr(buf):
        emit_pp_log(PP_MISS, "/rtl/foo.v", ms=2500.0, out_mib=3.2)
    line = buf.getvalue().strip()
    assert line.startswith("[hier-walk pp] pp-miss foo.v")
    assert "2500ms" in line
    assert "3.2MiB" in line


def test_emit_pp_log_brief_hides_mem_hit(monkeypatch):
    monkeypatch.setenv("HIERWALK_PP_LOG", "1")
    buf = io.StringIO()
    with redirect_stderr(buf):
        emit_pp_log(PP_MEM, "/rtl/foo.v")
    assert buf.getvalue() == ""


def test_emit_pp_log_all_shows_mem_hit(monkeypatch):
    monkeypatch.setenv("HIERWALK_PP_LOG", "2")
    buf = io.StringIO()
    with redirect_stderr(buf):
        emit_pp_log(PP_MEM, "/rtl/foo.v")
    assert "[hier-walk pp] pp-mem foo.v" in buf.getvalue()


def test_emit_pp_log_brief_disk_hit(monkeypatch):
    monkeypatch.setenv("HIERWALK_PP_LOG", "1")
    buf = io.StringIO()
    with redirect_stderr(buf):
        emit_pp_log(PP_DISK, "/rtl/bar.v", out_mib=1.0)
    assert "[hier-walk pp] pp-disk bar.v" in buf.getvalue()


def test_preprocess_log_level_default_brief(monkeypatch):
    monkeypatch.delenv("HIERWALK_PP_LOG", raising=False)
    from hierwalk.perf import preprocess_log_level

    assert preprocess_log_level() == 1