"""connect-coi periodic heartbeat (HIERWALK_PW_HEARTBEAT)."""

from __future__ import annotations

import time

from hierwalk.connectivity import _ConnectCoiHeartbeat


def test_connect_coi_heartbeat_disabled_without_env(monkeypatch):
    monkeypatch.delenv("HIERWALK_PW_HEARTBEAT", raising=False)
    emitted: list[str] = []

    with _ConnectCoiHeartbeat(
        total_checks=5,
        get_checks_done=lambda: 0,
        get_modules_cached=lambda: 0,
        on_emit=emitted.append,
    ):
        time.sleep(0.05)

    assert emitted == []


def test_connect_coi_heartbeat_emits_progress():
    emitted: list[str] = []
    done = 0

    with _ConnectCoiHeartbeat(
        total_checks=10,
        get_checks_done=lambda: done,
        get_modules_cached=lambda: 7,
        get_detail=lambda: "hierarchy_ready=4/10",
        on_emit=emitted.append,
        interval_sec=0.05,
    ):
        time.sleep(0.12)
        done = 3
        time.sleep(0.12)

    assert emitted
    joined = "\n".join(emitted)
    assert "connect-coi heartbeat" in joined
    assert "checks_done=3/10" in joined
    assert "modules_cached=7" in joined
    assert "hierarchy_ready=4/10" in joined
    assert "elapsed_sec=" in joined