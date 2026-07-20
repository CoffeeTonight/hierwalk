"""hgrep → path-walk handoff JSON structure."""

from __future__ import annotations

import json
from pathlib import Path

from hierwalk.connect.hierarchy_grep_gate import (
    gate_connect_check,
    prepare_hierarchy_grep_session,
)
from hierwalk.connect.hgrep_pathwalk_handoff import (
    check_gate_to_handoff,
    flat_rows_from_handoff_check,
    gate_from_handoff_check,
    load_pathwalk_handoff,
    path_walk_action_for_gate,
    write_handoff_from_gates,
    write_pathwalk_handoff,
)
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.index import DesignIndex


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_handoff_roundtrip_seed_fields(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    chk = ConnectivityCheck("top.u_a.out", "top.u_a.out", check_id="h1")
    gate = gate_connect_check(chk, session, top="top", index=DesignIndex({}))
    assert gate.status == "pass"
    assert path_walk_action_for_gate(gate) == "seed_and_text_coi"

    payload = check_gate_to_handoff(gate, chk, top="top")
    assert payload["path_walk_action"] == "seed_and_text_coi"
    assert payload["rows"]
    assert payload["rows"][0]["inst_leaf"]
    assert payload["rows"][0]["file"]

    rebuilt = gate_from_handoff_check(payload)
    assert rebuilt.status == "pass"
    assert len(rebuilt.rows) == len(gate.rows)
    assert rebuilt.rows[0].full_path == gate.rows[0].full_path
    assert rebuilt.rows[0].module == gate.rows[0].module

    rows = flat_rows_from_handoff_check(payload)
    assert rows[0].full_path == gate.rows[0].full_path


def test_write_handoff_batch_json(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; endmodule\n")
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    chk = ConnectivityCheck("top", "top", check_id="t0")
    gate = gate_connect_check(chk, session, top="top", index=DesignIndex({}))
    path = write_handoff_from_gates(
        [gate],
        [chk],
        top="top",
        path=tmp_path / "hgrep_pathwalk_handoff.json",
    )
    raw = load_pathwalk_handoff(path)
    assert raw["schema_version"] == 1
    assert raw["kind"] == "hgrep_pathwalk_handoff"
    assert "path_walk_contract" in raw
    assert raw["checks"][0]["check_id"] == "t0"
    assert "seed_and_text_coi" in raw["path_walk_contract"]


def test_gate_report_also_writes_handoff(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b1;
        endmodule
        module top;
          child u_b ();
        endmodule
        """,
    )
    session = prepare_hierarchy_grep_session([top_v], top="top")
    session.file_grep_index(wait=True)
    report = tmp_path / "conn.hgrep_gate.report"
    chk = ConnectivityCheck("top.u_b.out", "top.u_b.out", check_id="rep")
    gate_connect_check(
        chk,
        session,
        top="top",
        index=DesignIndex({}),
        report_path=report,
    )
    handoff = tmp_path / "hgrep_pathwalk_handoff.json"
    assert handoff.is_file()
    data = json.loads(handoff.read_text(encoding="utf-8"))
    assert data["checks"]
    assert data["checks"][0]["rows"]