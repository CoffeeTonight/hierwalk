"""Top name propagation from JSON / filelist / connect checks."""

from __future__ import annotations

import json
from pathlib import Path

from hierwalk.cache import resolve_effective_run_top
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.run_tests import parse_flat_run_suite


def test_resolve_effective_run_top_from_check_endpoints():
    top = resolve_effective_run_top(
        check_endpoints=["blabla.u0.a", "blabla.u0.z"],
    )
    assert top == "blabla"


def test_flat_suite_hoists_top_from_run_conn_check_block(tmp_path: Path):
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "top": "blabla",
                    "checks": [{"id": "t", "a": "blabla.a", "b": "blabla.z"}],
                    "output": "-",
                },
            }
        ),
        encoding="utf-8",
    )
    suite = parse_flat_run_suite(
        json.loads(run_json.read_text(encoding="utf-8")),
        base_dir=tmp_path,
    )
    assert suite.shared.top == "blabla"


def test_resolve_effective_run_top_prefers_json_over_checks():
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("other.a", "other.b"),),
        top="",
    )
    top = resolve_effective_run_top(
        cfg_top="blabla",
        connect_top=req.top,
        check_endpoints=[req.checks[0].endpoint_a],
    )
    assert top == "blabla"