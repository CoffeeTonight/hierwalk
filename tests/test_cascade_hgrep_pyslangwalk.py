"""Cascade connect_phase: hgrep then pyslangwalk on survivors only."""

from __future__ import annotations

from pathlib import Path

import pytest

pyslang = pytest.importorskip("pyslang")

from hierwalk.connect.shared.request import parse_connect_request_json
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import HGREP_THEN_PYSLANGWALK, parse_connect_phase_value


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_parse_cascade_phase_aliases():
    # Preferred: JSON array (ordered pipeline)
    assert parse_connect_phase_value(["hgrep", "pyslangwalk"]) == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value(["hgrep", "pyslang"]) == HGREP_THEN_PYSLANGWALK
    # Legacy string join
    assert parse_connect_phase_value("hgrep+pyslangwalk") == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value("hgrep-pyslangwalk") == HGREP_THEN_PYSLANGWALK
    assert parse_connect_phase_value("hgrep-then-pyslangwalk") == HGREP_THEN_PYSLANGWALK


def test_run_json_connect_phase_hgrep_not_default_both():
    """
    mode path-walk + connect_phase hgrep must NOT fall through to both/COI.

    Regression: parse_run_request_json used to ignore connect_phase entirely
    (default verification_phase=both → full text-COI on large designs).
    """
    from hierwalk.run_request import (
        extract_connect_phase_from_document,
        parse_run_request_json,
        resolve_effective_index_strategy,
        resolve_effective_run_mode,
    )

    # Top-level connect_phase
    cfg = parse_run_request_json(
        {
            "filelist": "filelist.f",
            "top": "top",
            "mode": "path-walk",
            "connect_phase": ["hgrep"],
            "connect": {"checks": [{"a": "top.a", "b": "top.b"}]},
        },
        base_dir=Path("."),
    )
    assert cfg.verification_phase == "hgrep"
    em = resolve_effective_run_mode(cfg)
    assert resolve_effective_index_strategy(cfg, em) == "hgrep"

    # Nested under connect{} (common user layout)
    assert (
        extract_connect_phase_from_document(
            {
                "mode": "path-walk",
                "connect": {
                    "connect_phase": ["hgrep"],
                    "checks": [{"a": "t.a", "b": "t.b"}],
                },
            }
        )
        == "hgrep"
    )
    cfg2 = parse_run_request_json(
        {
            "filelist": "filelist.f",
            "top": "top",
            "mode": "path-walk",
            "connect": {
                "connect_phase": "hgrep",
                "checks": [{"a": "top.a", "b": "top.b"}],
            },
        },
        base_dir=Path("."),
    )
    assert cfg2.verification_phase == "hgrep"
    assert resolve_effective_index_strategy(
        cfg2, resolve_effective_run_mode(cfg2)
    ) == "hgrep"


def test_array_hgrep_only_does_not_fall_through_to_both(tmp_path: Path):
    """``connect_phase: [\"hgrep\"]`` must be pure hgrep, not str(list)→both."""
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module top;
          logic s0, bus;
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "checks": [
                {"id": "ok", "a": "top.s0", "b": "top.bus"},
            ],
        }
    )
    logs: list[str] = []
    work = tmp_path / "db"
    batch, _, _ = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase=["hgrep"],
        connect_output_dir=work,
        no_cache=True,
        on_progress=logs.append,
    )
    joined = "\n".join(logs)
    assert "connect-hgrep begin" in joined or "hgrep-gate" in joined
    assert "cascade begin" not in joined
    assert "pyslangwalk" not in joined.lower()
    assert "connect-text-conn" not in joined
    assert all(r.mode == "hgrep" for r in batch.results)
    assert not (work / "pyslangwalk.report").exists()


def test_cascade_skips_pyslangwalk_on_hgrep_miss(tmp_path: Path):
    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module leaf (input logic [3:0] d, output logic [3:0] q);
          assign q = d;
        endmodule
        module top;
          logic [1:0][3:0] bus;
          logic [3:0] s0;
          leaf u0 (.d(bus[0]), .q(s0));
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "include_ff": True,
            "checks": [
                {"id": "ok", "a": "top.s0", "b": "top.bus"},
                {"id": "typo", "a": "top.NOPE", "b": "top.bus"},
            ],
        }
    )
    logs: list[str] = []
    work = tmp_path / "db"
    batch, _index, _state = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase="hgrep+pyslangwalk",
        connect_output_dir=work,
        connect_output_name="conn.tsv",
        no_cache=True,
        on_progress=logs.append,
    )
    by = {r.check_id: r for r in batch.results}
    assert "typo" in by
    assert not by["typo"].connected
    assert by["typo"].mode == "hgrep"
    # Survivor goes through pyslangwalk (mode may be pyslangwalk or +text).
    assert by["ok"].mode.startswith("pyslangwalk") or "pyslangwalk" in (
        by["ok"].note or ""
    )
    assert any(m.startswith("cascade begin") for m in logs)
    assert any("cascade hgrep pass=" in m for m in logs)
    assert any(m.startswith("cascade done") for m in logs)
    # Quiet cascade: no per-check hgrep-gate spam on progress channel
    assert not any("hgrep-gate check=" in m for m in logs)
    assert not any("hgrep-hie milestone" in m for m in logs)
    # Electrical report from pyslangwalk stage
    assert (work / "pyslangwalk.report").is_file()
    report = (work / "pyslangwalk.report").read_text(encoding="utf-8")
    # typo should not appear as electrical work on NOPE if filtered — survivors only
    # ok path may PASS electrical
    assert "top.s0" in report or "ok" in report


def test_parse_invalid_phase_raises():
    with pytest.raises(ValueError):
        parse_connect_phase_value("not-a-phase")
    with pytest.raises(ValueError):
        parse_connect_phase_value(["hgrep", "pyslang", "text"])


def test_cli_verification_phase_raises_on_garbage():
    """CLI phase helper must not swallow invalid phases into both."""
    from hierwalk.cli_execute import _verification_phase
    from hierwalk.run_request import RunConfig

    cfg = RunConfig(filelist="x.f", verification_phase="not-a-real-phase")
    with pytest.raises(ValueError):
        _verification_phase(cfg)


def test_reorder_equal_length_is_index_stable():
    from hierwalk.connect.pipeline.artifacts import reorder_connect_results_to_checks
    from hierwalk.connect.shared.request import ConnectivityCheck
    from hierwalk.models import ConnectEndpoint, ConnectResult

    def _r(cid: str, ok: bool) -> ConnectResult:
        ep = ConnectEndpoint(spec="t.a", inst_path="t", port_name="a")
        return ConnectResult(ep, ep, ok, "text", check_id=cid)

    checks = [
        ConnectivityCheck("t.a", "t.b", check_id="dup"),
        ConnectivityCheck("t.c", "t.d", check_id="dup"),
    ]
    results = [_r("dup", True), _r("dup", False)]
    ordered = reorder_connect_results_to_checks(checks, results)
    assert len(ordered) == 2
    # Equal-length → position preserved (not last-wins by id).
    assert ordered[0].connected is True
    assert ordered[1].connected is False


def test_fast_fail_reject_mode_is_hgrep():
    """Text-pipeline tier0 reject uses mode=hgrep (not unknown)."""
    from hierwalk.connect.hierarchy_grep_gate import (
        HierarchyGrepEndpointGate,
        _fast_fail_result,
    )
    from hierwalk.connect.shared.request import ConnectivityCheck

    eg = HierarchyGrepEndpointGate(
        spec="top.NOPE",
        hierarchy_input="top.NOPE",
        hierarchy="",
        port_tail="",
        ok=False,
        ambiguous=False,
        error="hierarchy miss",
        scoped_files=(),
        rows=(),
    )
    res = _fast_fail_result(
        ConnectivityCheck("top.NOPE", "top.b", check_id="typo"),
        spec="top.NOPE",
        gate=eg,
        miss_side="a",
    )
    assert not res.connected
    assert res.mode == "hgrep"
    assert any("hgrep-status reject" in str(n) for n in (res.walk_notes or ()))


def test_skip_hgrep_pregate_does_not_rebuild_grep_session(tmp_path: Path, monkeypatch):
    """Text pipeline with skip_hgrep_pregate must not prepare_hierarchy_grep_session."""
    from hierwalk.connect import hierarchy_grep_gate as hgg

    rtl = _write(
        tmp_path,
        "top.sv",
        """
        module top;
          logic a, b;
          assign b = a;
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "include_ff": True,
            "checks": [{"id": "c", "a": "top.a", "b": "top.b"}],
        }
    )
    calls = {"n": 0}
    real = hgg.prepare_hierarchy_grep_session

    def _count(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(hgg, "prepare_hierarchy_grep_session", _count)
    # Also patch path_walk import site if it binds locally — pipeline imports from gate.
    import hierwalk.path_walk as pw

    monkeypatch.setattr(
        "hierwalk.connect.hierarchy_grep_gate.prepare_hierarchy_grep_session",
        _count,
    )
    batch, _, _ = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase="text",
        connect_output_dir=tmp_path / "db",
        no_cache=True,
        skip_hgrep_pregate=True,
    )
    assert batch.results[0].connected
    assert calls["n"] == 0, f"unexpected prepare_hierarchy calls: {calls['n']}"


def test_cascade_merge_by_index_empty_check_ids(tmp_path: Path):
    """Empty/duplicate check_id must not collapse survivors in cascade merge."""
    _write(
        tmp_path,
        "top.sv",
        """
        module top;
          logic a, b, c, d;
          assign b = a;
          assign d = c;
        endmodule
        """,
    )
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "top",
            "include_ff": True,
            "checks": [
                {"a": "top.a", "b": "top.b"},
                {"a": "top.c", "b": "top.d"},
                {"a": "top.NOPE", "b": "top.b"},
            ],
        }
    )
    # No id fields → all check_id empty
    assert all(not (c.check_id or "") for c in req.checks)
    work = tmp_path / "db"
    batch, _, _ = run_path_walk_connect(
        req,
        fl,
        top="top",
        connect_phase=["hgrep", "pyslangwalk"],
        connect_output_dir=work,
        connect_output_name="conn.tsv",
        no_cache=True,
    )
    assert len(batch.results) == 3
    # Two survivors must both leave hgrep-only mode (merge by index).
    modes = [r.mode for r in batch.results]
    assert modes[2] == "hgrep"
    assert not batch.results[2].connected
    assert modes[0].startswith("pyslangwalk") or "pyslangwalk" in (
        batch.results[0].note or ""
    )
    assert modes[1].startswith("pyslangwalk") or "pyslangwalk" in (
        batch.results[1].note or ""
    )
    # Work-dir merged TSV written
    assert (work / "conn.tsv").is_file() or list(work.rglob("conn.tsv"))


def test_hgrep_fallback_note_not_zero_misses():
    """fallback with ok endpoints must not claim '0/N endpoint(s) miss'."""
    from hierwalk.connect.hierarchy_grep_gate import (
        HierarchyGrepCheckGate,
        HierarchyGrepEndpointGate,
        connect_result_from_hgrep_gate,
        hgrep_cascade_should_escalate,
    )
    from hierwalk.connect.shared.request import ConnectivityCheck

    eg = HierarchyGrepEndpointGate(
        spec="top.a",
        hierarchy_input="top.a",
        hierarchy="top",
        port_tail="a",
        ok=True,
        ambiguous=True,
        error="",
        scoped_files=("x.v",),
        rows=(),
    )
    gate = HierarchyGrepCheckGate(
        status="fallback",
        log_line="hgrep-gate check=c status=fallback reason=ambiguous",
        scoped_files=("x.v",),
        endpoint_gates=(eg, eg),
    )
    chk = ConnectivityCheck("top.a", "top.b", check_id="c")
    res = connect_result_from_hgrep_gate(chk, gate)
    assert not res.connected
    assert "fallback" in (res.note or "")
    assert "0/" not in (res.note or "") or "endpoints_ok=" in (res.note or "")
    assert "endpoint(s) miss" not in (res.note or "")
    assert any(str(n).startswith("hgrep-status fallback") for n in res.walk_notes)
    assert hgrep_cascade_should_escalate(res)


def test_hgrep_reject_does_not_escalate():
    from hierwalk.connect.hierarchy_grep_gate import (
        HierarchyGrepCheckGate,
        HierarchyGrepEndpointGate,
        connect_result_from_hgrep_gate,
        hgrep_cascade_should_escalate,
    )
    from hierwalk.connect.shared.request import ConnectivityCheck

    eg_bad = HierarchyGrepEndpointGate(
        spec="top.NOPE",
        hierarchy_input="top.NOPE",
        hierarchy="",
        port_tail="",
        ok=False,
        ambiguous=False,
        error="hierarchy miss",
        scoped_files=(),
        rows=(),
    )
    eg_ok = HierarchyGrepEndpointGate(
        spec="top.b",
        hierarchy_input="top.b",
        hierarchy="top",
        port_tail="b",
        ok=True,
        ambiguous=False,
        error="",
        scoped_files=("x.v",),
        rows=(),
    )
    gate = HierarchyGrepCheckGate(
        status="reject",
        log_line="status=reject reason=grep-miss",
        endpoint_gates=(eg_bad, eg_ok),
    )
    res = connect_result_from_hgrep_gate(
        ConnectivityCheck("top.NOPE", "top.b", check_id="typo"),
        gate,
    )
    assert not res.connected
    assert "reject" in (res.note or "")
    assert not hgrep_cascade_should_escalate(res)


def test_suite_schedules_cascade_phase(tmp_path: Path):
    from hierwalk.run_tests import (
        build_test_run_configs,
        expand_suite_verification_plan,
        parse_flat_run_suite,
    )

    rtl = _write(tmp_path, "top.sv", "module top; endmodule\n")
    (tmp_path / "filelist.f").write_text("top.sv\n", encoding="utf-8")
    doc = {
        "filelist": "filelist.f",
        "top": "top",
        "index-cwd": str(tmp_path),
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "connect_phase": ["hgrep", "pyslangwalk"],
            "checks": [{"id": "c", "a": "top.a", "b": "top.b"}],
        },
    }
    suite = parse_flat_run_suite(doc, raw_text=None, base_dir=tmp_path)
    plan = build_test_run_configs(suite, doc, base_dir=tmp_path)
    expanded = expand_suite_verification_plan(plan)
    assert len(expanded) == 1
    _e, cfg = expanded[0]
    assert cfg.verification_phase == HGREP_THEN_PYSLANGWALK
    assert cfg.mode == "check-pyslangwalk"
