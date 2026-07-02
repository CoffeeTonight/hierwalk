"""Performance helper tests."""

from __future__ import annotations

import os

from hierwalk.perf import (
    body_param_scan_max,
    effective_low_memory,
    low_memory_auto_threshold,
    pw_inst_resolve_tier1_max,
    pw_fl_shell_max,
    pw_module_file_cap,
    pw_tier0_global_scan_max,
    text_grep_prewarm_enabled,
)


def test_effective_low_memory_explicit():
    assert effective_low_memory(explicit=True, num_sources=10) is True


def test_effective_low_memory_auto_threshold(monkeypatch):
    monkeypatch.delenv("HIERWALK_LOW_MEMORY_AUTO", raising=False)
    assert low_memory_auto_threshold() == 1500
    assert effective_low_memory(explicit=False, num_sources=1499) is False
    assert effective_low_memory(explicit=False, num_sources=1500) is True


def test_effective_low_memory_auto_disabled(monkeypatch):
    monkeypatch.setenv("HIERWALK_LOW_MEMORY_AUTO", "0")
    assert low_memory_auto_threshold() == 0
    assert effective_low_memory(explicit=False, num_sources=99999) is False


def test_pw_tier0_caps_env(monkeypatch):
    monkeypatch.delenv("HIERWALK_PW_MODULE_FILE_CAP", raising=False)
    monkeypatch.delenv("HIERWALK_PW_TIER0_GLOBAL_MAX", raising=False)
    monkeypatch.delenv("HIERWALK_PW_TIER1_MAX", raising=False)
    assert pw_fl_shell_max() == 12
    assert pw_module_file_cap() == 32
    assert pw_tier0_global_scan_max() == 128
    assert pw_inst_resolve_tier1_max("confident") == 12
    assert pw_inst_resolve_tier1_max("recovery") == 24
    monkeypatch.setenv("HIERWALK_PW_MODULE_FILE_CAP", "8")
    assert pw_module_file_cap() == 8


def test_text_grep_prewarm_opt_in(monkeypatch):
    monkeypatch.delenv("HIERWALK_TEXT_GREP_PREWARM", raising=False)
    assert text_grep_prewarm_enabled() is False
    monkeypatch.setenv("HIERWALK_TEXT_GREP_PREWARM", "1")
    assert text_grep_prewarm_enabled() is True


def test_body_param_scan_max_env(monkeypatch):
    monkeypatch.delenv("HIERWALK_BODY_PARAM_SCAN_MAX", raising=False)
    assert body_param_scan_max() == 512 * 1024
    monkeypatch.setenv("HIERWALK_BODY_PARAM_SCAN_MAX", "0")
    assert body_param_scan_max() == 0
    monkeypatch.setenv("HIERWALK_BODY_PARAM_SCAN_MAX", "4096")
    assert body_param_scan_max() == 4096