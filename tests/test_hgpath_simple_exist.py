"""Optional simple_exist mode: slash paths → \\bsegment\\b in parent RTL."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hg_core.run_config import parse_hg_run_config
from hgpath.flat_db import load_or_build_flat_db
from hgpath.simple_exist import resolve_simple_exist, slash_spec_to_dots
from hgpath.tree_db import TreeDb, resolve_tree_db_path
from hgpath.walker import resolve_with_tree

_STRESS_RTL = Path(__file__).resolve().parents[2] / "hgrep_demo" / "top_ifndef_stress.v"


@pytest.fixture(scope="module")
def stress_session():
    _db, session = load_or_build_flat_db(
        [str(_STRESS_RTL)],
        top="top",
        work_dir=Path(__file__).resolve().parent / "_simple_exist_work",
    )
    return session


def test_slash_spec_to_dots():
    assert slash_spec_to_dots("/top/u_A/out") == "top.u_A.out"
    assert slash_spec_to_dots("top.u_A.out") == "top.u_A.out"


@pytest.mark.parametrize(
    "spec",
    (
        "/top/u_A",
        "/top/u_deep",
        "/top/u_mid/u_sub",
    ),
)
def test_resolve_simple_exist_pass(stress_session, spec: str):
    result = resolve_simple_exist(stress_session, spec, top="top")
    assert result.get("ok") is True, result.get("error")
    assert result.get("simple_exist") is True


def test_resolve_simple_exist_missing_inst(stress_session):
    result = resolve_simple_exist(stress_session, "/top/u_totally_missing", top="top")
    assert result.get("ok") is False
    assert "not found" in (result.get("error") or "").lower()


def test_simple_exist_preprocess_comment_only(stress_session, monkeypatch):
    from hierwalk import inst_scan

    slim_calls: list[str] = []
    orig = inst_scan.slim_body_for_instance_scan

    def _wrap(body: str) -> str:
        slim_calls.append("called")
        return orig(body)

    monkeypatch.setattr(inst_scan, "slim_body_for_instance_scan", _wrap)

    result = resolve_simple_exist(stress_session, "/top/u_mid/u_sub", top="top")
    assert result.get("ok") is True, result.get("error")
    assert slim_calls == []


def test_resolve_with_tree_simple_mode(stress_session, tmp_path: Path):
    tree = TreeDb(work_dir=tmp_path, path=resolve_tree_db_path(tmp_path))
    logs: list[str] = []
    entry = resolve_with_tree(
        "/top/u_A",
        top="top",
        session=stress_session,
        tree=tree,
        on_log=logs.append,
        simple_exist=True,
    )
    assert entry.ok
    assert any("mode=simple" in line and "preprocess=comments-only" in line for line in logs)


def test_resolve_with_tree_full_when_flag_off(stress_session, tmp_path: Path):
    tree = TreeDb(work_dir=tmp_path, path=resolve_tree_db_path(tmp_path))
    logs: list[str] = []
    resolve_with_tree(
        "/top/u_A",
        top="top",
        session=stress_session,
        tree=tree,
        on_log=logs.append,
        simple_exist=False,
    )
    assert any("mode=full" in line for line in logs)


def test_parse_hg_run_config_simple_exist_json(tmp_path: Path):
    doc = {"top": "top", "simple_exist": True, "checks": [{"a": "/top/u_A", "b": "/top/u_B"}]}
    cfg = parse_hg_run_config(doc, tmp_path)
    assert cfg.simple_exist is True
    assert str(cfg.checks[0].endpoint_a) == "/top/u_A"


def test_parse_hg_run_config_simple_exist_cli_overrides(tmp_path: Path):
    doc = {"top": "top", "simple_exist": False, "checks": []}
    cfg = parse_hg_run_config(doc, tmp_path, simple_exist_cli=True)
    assert cfg.simple_exist is True


def test_hgpath_cli_simple_exist_e2e():
    demo = _STRESS_RTL.parent
    with tempfile.TemporaryDirectory(prefix="hg_simple_") as td:
        work = Path(td)
        checks = work / "checks.json"
        checks.write_text(
            json.dumps(
                {
                    "filelist": str(demo / "fl_ifndef_stress.f"),
                    "top": "top",
                    "index-cwd": str(demo),
                    "simple_exist": True,
                    "checks": [
                        {"id": "a", "a": "/top/u_A", "b": "/top/u_A"},
                        {"id": "b", "a": "/top/u_totally_missing", "b": "/top/u_totally_missing"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        proc = subprocess.run(
            [sys.executable, "-m", "hgpath.cli", "--work-dir", str(work), "--checks", str(checks)],
            cwd=str(demo),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
        report = (work / "hgpath.report").read_text(encoding="utf-8")
        assert "simple_exist                 on (preprocess: comments-only)" in report
        assert "FAIL" in report