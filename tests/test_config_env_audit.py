"""Config env audit logging."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.config_env_audit import (
    format_config_env_audit_lines,
    format_verilog_defines_audit_lines,
)
from hierwalk.preprocess import _define_active
from hierwalk.run_request import apply_config_env_from_document


def test_format_config_env_audit_shows_json_env_and_policy(monkeypatch):
    monkeypatch.delenv("HIERWALK_LAZY", raising=False)
    monkeypatch.delenv("HIERWALK_LAZY_IFDEF", raising=False)
    doc = {
        "defines": {"ABC": "1"},
        "env": {"HIERWALK_LAZY_IFDEF": "1"},
    }
    applied = apply_config_env_from_document(doc)
    lines = format_config_env_audit_lines(doc, json_env_applied=applied)
    text = "\n".join(lines)
    assert "JSON env block declared" in text
    assert "HIERWALK_LAZY_IFDEF=1" in text
    assert "source=json:env" in text
    assert "verilog-defines from JSON top-level: ABC=1" in text
    assert "active-at-index" in text
    assert "index-ifdef-policy" in text


def test_define_active_for_ifndef_semantics():
    assert _define_active("ABC", {}) is False
    assert _define_active("ABC", {"ABC": "0"}) is False
    assert _define_active("ABC", {"ABC": ""}) is False
    assert _define_active("ABC", {"ABC": "false"}) is False
    assert _define_active("ABC", {"ABC": "1"}) is True


def test_verilog_defines_audit_merged_active_flags():
    lines = format_verilog_defines_audit_lines(
        effective_defines={"ABC": "0", "SYNTH": "1"},
        json_defines={"ABC": "0"},
        connect_defines={},
    )
    text = "\n".join(lines)
    assert "from JSON defines" in text
    assert "ABC='0'" in text
    assert "SYNTH='1'" in text
    assert "active=0" in text
    assert "active=1" in text
    assert "`ifdef" not in text


def test_cli_emits_verilog_defines_after_filelist(tmp_path: Path):
    (tmp_path / "top.v").write_text(
        """
module top;
`ifndef ABC
 DEF u_DEF (.QW());
`endif
endmodule
module DEF(output QW); endmodule
""",
        encoding="utf-8",
    )
    (tmp_path / "design.f").write_text("+define+SYNTH=1\ntop.v\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "top",
                "defines": {"ABC": "0"},
                "no_cache": True,
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert "verilog-defines:" in proc.stderr
    assert "ABC='0'" in proc.stderr
    assert "SYNTH='1'" in proc.stderr
    assert "top.u_DEF" in proc.stdout


def test_cli_emits_config_env_audit(tmp_path: Path):
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "missing.f",
                "env": {"HIERWALK_LAZY_IFDEF": "1"},
                "defines": {},
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["hier-walk", str(run_json)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert "config-env:" in proc.stderr
    assert "HIERWALK_LAZY_IFDEF=1" in proc.stderr
    assert "index-ifdef-policy" in proc.stderr