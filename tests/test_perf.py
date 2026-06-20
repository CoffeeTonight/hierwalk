"""Performance helper tests."""

from __future__ import annotations

import os

from hierwalk.perf import (
    body_param_scan_max,
    effective_low_memory,
    low_memory_auto_threshold,
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


def test_body_param_scan_max_env(monkeypatch):
    monkeypatch.delenv("HIERWALK_BODY_PARAM_SCAN_MAX", raising=False)
    assert body_param_scan_max() == 512 * 1024
    monkeypatch.setenv("HIERWALK_BODY_PARAM_SCAN_MAX", "0")
    assert body_param_scan_max() == 0
    monkeypatch.setenv("HIERWALK_BODY_PARAM_SCAN_MAX", "4096")
    assert body_param_scan_max() == 4096