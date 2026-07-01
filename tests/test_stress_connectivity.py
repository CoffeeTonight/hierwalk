"""Deep hierarchy connectivity stress tests (random RTL generator)."""

from __future__ import annotations

import re

import pytest

import subprocess

from hierwalk.connect.shared.request import load_connect_request
from hierwalk.stress_gen import (
    EXTREME_CONFIG,
    STANDARD_CONFIG,
    _CONSTRUCT_NAMES,
    build_stress_connect_request,
    format_stress_report,
    generate_stress_design,
    run_stress_batch,
    run_stress_trial,
    write_stress_artifacts,
)


def test_stress_design_extreme_zigzag():
    design = generate_stress_design(seed=99, depth=20, branch_factor=8)
    assert design.depth == 20
    assert design.branch_factor == 8
    assert design.layout == "zigzag"
    assert design.num_rungs >= 4
    assert design.tunnel_depth >= 2
    assert design.top == "stress_top"
    assert design.endpoint_a == "stress_top.probe_in"
    assert design.endpoint_b == "stress_top.probe_out"
    assert "u_rung" in design.spine_path
    assert design.endpoint_port_inst[1].startswith("stress_top.u_rung")
    assert "u_pong" in design.endpoint_cross[1]
    assert len(design.construct_schedule) >= 20
    for name in design.construct_schedule:
        assert name in _CONSTRUCT_NAMES
    assert len(design.files) >= 5
    assert any(
        c.startswith(("casex", "casez", "mdarray", "for_array"))
        for c in design.construct_schedule
    )
    assert any(
        c in design.construct_schedule
        for c in ("generate_for", "gen_nested", "if_generate", "param_ifgenerate")
    )
    assert "BASE * STRIDE" in design.verilog or "STRIDE" in design.verilog
    assert "hop_" in design.verilog


def test_stress_trial_extreme_connected():
    _design, trial = run_stress_trial(seed=1, depth=20, branch_factor=8)
    assert trial.connected
    assert trial.connected_port_port
    assert trial.connected_port_inst
    assert trial.connected_cross
    assert trial.layout == "zigzag"
    assert trial.depth == 20
    assert trial.instance_rows >= 80
    assert trial.file_count >= 5
    assert "module(s)" in trial.modules_parsed_note_pp
    modules_parsed = int(
        re.search(r"(\d+) module", trial.modules_parsed_note_pp).group(1)
    )
    assert modules_parsed >= 5


def test_stress_trial_standard_profile_still_works():
    _design, trial = run_stress_trial(
        seed=1,
        depth=10,
        branch_factor=5,
        config=STANDARD_CONFIG,
    )
    assert trial.connected
    assert trial.layout == "linear"
    assert trial.file_count == 1
    assert trial.connected_cross


@pytest.mark.stress
def test_stress_batch_ten_trials_extreme():
    results = run_stress_batch(trials=10, base_seed=20260613, config=EXTREME_CONFIG)
    assert len(results) == 10
    assert all(r.connected for r in results), format_stress_report(results)
    assert all(r.connected_port_port for r in results)
    assert all(r.connected_port_inst for r in results)
    assert all(r.connected_cross for r in results)
    assert all(r.layout == "zigzag" for r in results)
    assert all(17 <= r.depth <= 23 for r in results)
    assert all(6 <= r.branch_factor <= 10 for r in results)
    assert all(r.total_sec < 120.0 for r in results)
    report = format_stress_report(results)
    assert "avg total_ms" in report
    assert "xz" in report.splitlines()[0]


def test_stress_connect_request_includes_missing_hierarchy():
    design = generate_stress_design(seed=42, depth=10, branch_factor=5, config=STANDARD_CONFIG)
    req = build_stress_connect_request(design)
    assert req.top == design.top
    assert req.defines == design.defines
    assert req.include_ff
    ids = [c.check_id for c in req.checks]
    assert ids == ["port_port", "port_inst", "cross_hierarchy", "missing_hierarchy"]
    missing = next(c for c in req.checks if c.check_id == "missing_hierarchy")
    assert "u_missing" in missing.endpoint_a


def test_stress_artifacts_connect_json_end_to_end(tmp_path):
    design = generate_stress_design(seed=42, depth=10, branch_factor=5, config=STANDARD_CONFIG)
    out = tmp_path / "stress_seed42"
    paths = write_stress_artifacts(design, out)
    assert "connect.json" in paths
    connect_path = paths["connect.json"]
    req = load_connect_request(connect_path)
    assert req.top == "stress_top"
    assert len(req.checks) == 4

    proc = subprocess.run(
        [
            "hier-walk",
            str(out / "filelist.f"),
            "--top",
            req.top,
            "--no-cache",
            "--check-connect-batch",
            connect_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0].startswith("check_id\t")
    by_id = {ln.split("\t", 1)[0]: ln for ln in lines[1:]}
    assert "True" in by_id["port_port"]
    assert "True" in by_id["port_inst"]
    assert "True" in by_id["cross_hierarchy"]
    assert "False" in by_id["missing_hierarchy"]
    assert "hierarchy not found" in by_id["missing_hierarchy"]