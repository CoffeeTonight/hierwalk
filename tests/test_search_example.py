"""hier-walk --example search JSON generator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.run_request import loads_json_document
from hierwalk.search_example import SEARCH_EXAMPLE_FILENAME, search_example_text
from hierwalk.search_spec import resolve_search_spec


def test_search_example_text_parses():
    data = loads_json_document(search_example_text())
    spec = resolve_search_spec(data)
    assert spec is not None
    assert spec.instance
    assert spec.path
    assert spec.hierarchy_path
    assert spec.case_insensitive
    assert spec.search_module
    assert spec.search_subtree
    assert "stress_top.probe_in" in spec.hierarchy_path


def test_cli_example_writes_default_file(tmp_path: Path):
    proc = subprocess.run(
        ["hier-walk", "--example"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    out = tmp_path / SEARCH_EXAMPLE_FILENAME
    assert out.is_file()
    assert "structured search" in proc.stderr.lower() or "wrote search example" in proc.stderr.lower()
    data = loads_json_document(out.read_text(encoding="utf-8"))
    assert data["mode"] == "search"
    assert isinstance(data["search"], dict)


def test_cli_example_stdout():
    proc = subprocess.run(
        ["hier-walk", "--example", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    assert '"mode": "search"' in proc.stdout
    assert '"instance"' in proc.stdout


def test_cli_help_lists_example_flag():
    proc = subprocess.run(
        ["hier-walk", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--example" in proc.stdout