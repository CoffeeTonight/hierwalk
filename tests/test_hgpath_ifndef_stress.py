"""Stress: ifndef + pending_cell variants via unit resolve and hgpath/hgconn e2e."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hierwalk.hierarchy_grep import HierarchyGrepSession, _inst_child_module

_STRESS_RTL = Path(__file__).resolve().parents[2] / "hgrep_demo" / "top_ifndef_stress.v"
_DEMO = _STRESS_RTL.parent
_MATRIX = Path(__file__).resolve().parent / "fixtures" / "parse_matrix_soc.v"

# (inst, expected_child, resolve_spec, should_ok)
_STRESS_CASES = (
    ("u_A", "LeafA", "top.u_A.out", True),
    ("u_deep", "LeafA", "top.u_deep.out", True),
    ("u_B", "LeafB", "top.u_B.out", True),
    ("u_glue_a", "LeafA", "top.u_glue_a.out", True),
    ("u_glue_b", "LeafB", "top.u_glue_b.out", True),
    ("u_blk", "LeafA", "top.u_blk.out", True),
    ("u_attr", "LeafA", "top.u_attr.out", True),
    ("u_real", "LeafA", "top.u_real.out", True),
    ("u_esc", "BUF_ESC", "top.u_esc.o", True),
    ("u_mid", "MidA", "top.u_mid.out", True),
    ("u_sub", "Sub", "top.u_mid.u_sub.out", True),
    ("u_parm", "LeafA", "top.u_parm.out", True),
    ("u_ports", "LeafA", "top.u_ports.out", True),
    ("u_dead", None, "top.u_dead.out", False),
    ("u_fake", None, "top.u_fake.out", False),
)

_MATRIX_HIER_CHECKS = (
    ("soc_u_a", "SOC_TOP.u_A"),
    ("soc_cpusystem", "SOC_TOP.u_cpusystem_top"),
    ("soc_wrap", "SOC_TOP.u_wrap"),
    ("soc_bcd", "SOC_TOP.u_BCD"),
    ("soc_gen0", "SOC_TOP.gen_blk[0].u_BCD_gen"),
    ("soc_nest", "SOC_TOP.outer[0].inner[1].u_nest"),
    ("soc_port_ifdef", "SOC_TOP.port_ifndef_blk.u_DEF"),
)


@pytest.fixture(scope="module")
def stress_session() -> HierarchyGrepSession:
    return HierarchyGrepSession.from_rtl_paths(
        [str(_STRESS_RTL)],
        build_file_index_background=False,
    )


@pytest.mark.parametrize(
    "inst,child,spec,should_ok",
    _STRESS_CASES,
    ids=[c[0] for c in _STRESS_CASES],
)
def test_stress_inst_child_and_resolve(
    stress_session: HierarchyGrepSession,
    inst: str,
    child: str | None,
    spec: str,
    should_ok: bool,
):
    rtl = _STRESS_RTL.read_text(encoding="utf-8")
    got_child = _inst_child_module(rtl, inst)
    if child is None:
        assert got_child is None
    else:
        assert got_child == child

    result = stress_session.resolve(spec, top="top")
    assert result.get("ok") is should_ok, result.get("error")


def test_stress_cross_port_connectivity(stress_session: HierarchyGrepSession):
    """``out`` and ``aux`` on same LeafA instance (assign aux = out)."""
    a = stress_session.resolve("top.u_A.out", top="top")
    b = stress_session.resolve("top.u_A.aux", top="top")
    assert a.get("ok") and b.get("ok")


def _run_hg_cli(module: str, work_dir: Path, checks_json: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", module, "--work-dir", str(work_dir), "--checks", str(checks_json)],
        cwd=str(_DEMO),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_stress_hgpath_hgconn_e2e():
    with tempfile.TemporaryDirectory(prefix="hg_stress_") as td:
        work = Path(td)
        proc_p = _run_hg_cli("hgpath.cli", work, _DEMO / "RUN_ifndef_stress.json")
        assert proc_p.returncode == 0, proc_p.stderr + proc_p.stdout
        report = (work / "hgpath.report").read_text(encoding="utf-8")
        assert "success: 100.0%" in report
        assert "fail: 0" in report

        proc_c = _run_hg_cli("hgconn.cli", work, _DEMO / "RUN_ifndef_stress.json")
        assert proc_c.returncode == 0, proc_c.stderr + proc_c.stdout
        conn_report = (work / "hgconn.report").read_text(encoding="utf-8")
        assert "connected: 13" in conn_report or "success: 100.0%" in conn_report


@pytest.mark.parametrize("check_id,spec", _MATRIX_HIER_CHECKS, ids=[c[0] for c in _MATRIX_HIER_CHECKS])
def test_parse_matrix_hgpath_resolve(check_id: str, spec: str):
    """Combination-matrix RTL axes through hgpath-style hierarchy resolve."""
    with tempfile.TemporaryDirectory(prefix="hg_matrix_") as td:
        rtl = Path(td) / "soc.v"
        rtl.write_text(_MATRIX.read_text(encoding="utf-8"), encoding="utf-8")
        session = HierarchyGrepSession.from_rtl_paths(
            [str(rtl)],
            build_file_index_background=False,
        )
        result = session.resolve(spec, top="SOC_TOP")
        assert result.get("ok") is True, result.get("error")