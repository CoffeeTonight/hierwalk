"""Serialize hgrep gate outcomes for path-walk consumption.

Contract (path-walk must honor):

* ``path_walk_action=seed_and_text_coi`` — seed ``rows`` + open only
  ``scoped_files``, then text-COI (hgrep fast path).
* ``path_walk_action=full_path_walk`` — optional scoped pool from
  ``scoped_files``; run normal hierarchy walk + text-COI.
* ``path_walk_action=reject`` — hierarchy miss; do not path-walk.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.connect.hierarchy_grep_gate import (
    HierarchyGrepCheckGate,
    HierarchyGrepEndpointGate,
    seed_gate_inst_chain,
    scoped_sources_for_gate,
)
from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.hierarchy_grep import abs_rtl_path
from hierwalk.models import FlatRow

HANDOFF_JSON_NAME = "hgrep_pathwalk_handoff.json"
HANDOFF_SCHEMA_VERSION = 1

_HANDOFF_LOCK = threading.Lock()

PATH_WALK_CONTRACT = {
    "seed_and_text_coi": (
        "seed rows into PathWalkState.rows_by_path; "
        "text COI only opens scoped_files"
    ),
    "full_path_walk": (
        "optional scoped_sources pool from scoped_files; "
        "full hierarchy ensure_path + text COI"
    ),
    "reject": "hierarchy miss; skip path-walk; emit connect fail",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def resolve_pathwalk_handoff_path(
    work_or_report: str | Path | None,
) -> Optional[Path]:
    """Next to gate report, or under work dir as ``hgrep_pathwalk_handoff.json``."""
    if work_or_report is None:
        return None
    path = Path(work_or_report).expanduser()
    if path.suffix in (".report", ".txt", ".log"):
        return path.parent / HANDOFF_JSON_NAME
    if path.is_dir() or not path.suffix:
        return path / HANDOFF_JSON_NAME
    return path.parent / HANDOFF_JSON_NAME


def path_walk_action_for_gate(gate: HierarchyGrepCheckGate) -> str:
    """Map gate status → path-walk action key."""
    if gate.fast_fail_result is not None or gate.status == "reject":
        return "reject"
    if gate.status == "pass" and gate.use_grep_fast_path:
        return "seed_and_text_coi"
    return "full_path_walk"


def _json_str(value: Any) -> str:
    """Coerce paths / other objects to plain str for JSON handoff."""
    if value is None:
        return ""
    if isinstance(value, Path):
        return str(value)
    return str(value)


def flat_row_to_dict(row: FlatRow) -> Dict[str, Any]:
    return {
        "full_path": _json_str(row.full_path),
        "inst_leaf": _json_str(row.inst_leaf),
        "module": _json_str(row.module),
        "depth": int(row.depth),
        "parent_path": _json_str(row.parent_path) if row.parent_path else None,
        "file": abs_rtl_path(row.file) if row.file else "",
        "stop_reason": _json_str(row.stop_reason or ""),
        # via_filelist / filelist_chain may be Path on path-walk rows
        "via_filelist": _json_str(row.via_filelist or ""),
        "filelist_chain": _json_str(row.filelist_chain or ""),
        "param_ctx": {
            _json_str(k): _json_str(v) for k, v in dict(row.param_ctx or {}).items()
        },
        "param_ctx_folded": bool(row.param_ctx_folded),
        "refine_status": _json_str(row.refine_status or "grep"),
        "activation": _json_str(row.activation or ""),
        "walk_note": _json_str(row.walk_note or ""),
    }


def flat_row_from_dict(data: Mapping[str, Any]) -> FlatRow:
    parent = data.get("parent_path")
    if parent is not None:
        parent = str(parent) if parent else None
    return FlatRow(
        full_path=str(data.get("full_path", "")),
        inst_leaf=str(data.get("inst_leaf", "")),
        module=str(data.get("module", "")),
        depth=int(data.get("depth", 0) or 0),
        parent_path=parent,
        file=abs_rtl_path(str(data.get("file", "") or "")),
        stop_reason=str(data.get("stop_reason", "") or ""),
        via_filelist=str(data.get("via_filelist", "") or ""),
        filelist_chain=str(data.get("filelist_chain", "") or ""),
        param_ctx=dict(data.get("param_ctx") or {}),
        param_ctx_folded=bool(data.get("param_ctx_folded", False)),
        refine_status=str(data.get("refine_status", "") or "grep"),
        activation=str(data.get("activation", "") or ""),
        walk_note=str(data.get("walk_note", "") or ""),
    )


def _endpoint_to_dict(ep: HierarchyGrepEndpointGate) -> Dict[str, Any]:
    return {
        "spec": ep.spec,
        "hierarchy_input": ep.hierarchy_input,
        "hierarchy": ep.hierarchy,
        "port_tail": ep.port_tail,
        "ok": ep.ok,
        "ambiguous": ep.ambiguous,
        "error": ep.error or "",
        "scoped_files": [abs_rtl_path(f) for f in ep.scoped_files if f],
        "rows": [flat_row_to_dict(r) for r in ep.rows],
    }


def _endpoint_from_dict(data: Mapping[str, Any]) -> HierarchyGrepEndpointGate:
    return HierarchyGrepEndpointGate(
        spec=str(data.get("spec", "")),
        hierarchy_input=str(data.get("hierarchy_input", "") or data.get("hierarchy", "")),
        hierarchy=str(data.get("hierarchy", "")),
        port_tail=str(data.get("port_tail", "") or ""),
        ok=bool(data.get("ok")),
        ambiguous=bool(data.get("ambiguous")),
        error=str(data.get("error", "") or ""),
        scoped_files=tuple(
            abs_rtl_path(f) for f in (data.get("scoped_files") or []) if f
        ),
        rows=tuple(flat_row_from_dict(r) for r in (data.get("rows") or [])),
    )


def check_gate_to_handoff(
    gate: HierarchyGrepCheckGate,
    chk: ConnectivityCheck,
    *,
    top: str = "",
) -> Dict[str, Any]:
    """One check → path-walk handoff dict."""
    action = path_walk_action_for_gate(gate)
    return {
        "check_id": str(chk.check_id or ""),
        "endpoint_a": str(chk.endpoint_a),
        "endpoint_b": str(chk.endpoint_b),
        "top": top,
        "status": gate.status,
        "path_walk_action": action,
        "use_grep_fast_path": bool(gate.use_grep_fast_path),
        "scoped_files": [abs_rtl_path(f) for f in gate.scoped_files if f],
        "rows": [flat_row_to_dict(r) for r in gate.rows],
        "endpoints": [_endpoint_to_dict(g) for g in gate.endpoint_gates],
        "log_line": gate.log_line or "",
    }


def gate_from_handoff_check(data: Mapping[str, Any]) -> HierarchyGrepCheckGate:
    """Rebuild in-memory gate for seed_gate_inst_chain / text_check_from_gate."""
    status = str(data.get("status", "fallback") or "fallback")
    return HierarchyGrepCheckGate(
        status=status,
        log_line=str(data.get("log_line", "") or ""),
        scoped_files=tuple(
            abs_rtl_path(f) for f in (data.get("scoped_files") or []) if f
        ),
        rows=tuple(flat_row_from_dict(r) for r in (data.get("rows") or [])),
        endpoint_gates=tuple(
            _endpoint_from_dict(e) for e in (data.get("endpoints") or [])
        ),
        fast_fail_result=None,
    )


def build_handoff_batch(
    *,
    top: str,
    checks: Sequence[Dict[str, Any]],
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "kind": "hgrep_pathwalk_handoff",
        "written_at": _utc_now_iso(),
        "top": top,
        "path_walk_contract": dict(PATH_WALK_CONTRACT),
        "check_count": len(checks),
        "action_counts": _action_counts(checks),
        "checks": list(checks),
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _action_counts(checks: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {
        "seed_and_text_coi": 0,
        "full_path_walk": 0,
        "reject": 0,
        "other": 0,
    }
    for c in checks:
        action = str(c.get("path_walk_action", "") or "other")
        if action in counts:
            counts[action] += 1
        else:
            counts["other"] += 1
    return counts


def write_pathwalk_handoff(
    path: str | Path,
    *,
    top: str,
    checks: Sequence[Dict[str, Any]],
    extra: Optional[Mapping[str, Any]] = None,
) -> Path:
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_handoff_batch(top=top, checks=checks, extra=extra)

    def _default(obj: Any) -> str:
        if isinstance(obj, Path):
            return str(obj)
        return str(obj)

    text = json.dumps(payload, indent=2, ensure_ascii=False, default=_default) + "\n"
    with _HANDOFF_LOCK:
        out.write_text(text, encoding="utf-8")
    return out


def upsert_pathwalk_handoff_check(
    path: str | Path,
    check_payload: Mapping[str, Any],
    *,
    top: str = "",
) -> Path:
    """Insert/replace one check in the handoff JSON (stream-friendly)."""
    out = Path(path).expanduser().resolve()
    with _HANDOFF_LOCK:
        checks: List[Dict[str, Any]] = []
        existing_top = top
        if out.is_file():
            try:
                raw = json.loads(out.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    existing_top = str(raw.get("top") or top or "")
                    prev = raw.get("checks") or []
                    if isinstance(prev, list):
                        checks = [c for c in prev if isinstance(c, Mapping)]
            except (OSError, json.JSONDecodeError):
                checks = []
        cid = str(check_payload.get("check_id", ""))
        rest = [
            c
            for c in checks
            if str(c.get("check_id", "")) != cid or not cid
        ]
        rest.append(dict(check_payload))
        payload = build_handoff_batch(top=existing_top or top, checks=rest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return out


def load_pathwalk_handoff(path: str | Path) -> Dict[str, Any]:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("handoff JSON must be an object")
    return dict(raw)


def write_handoff_from_gates(
    gates: Sequence[Optional[HierarchyGrepCheckGate]],
    checks: Sequence[ConnectivityCheck],
    *,
    top: str,
    path: str | Path,
    extra: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Batch-write gates aligned with *checks* (None gate → full_path_walk)."""
    payloads: List[Dict[str, Any]] = []
    for chk, gate in zip(checks, gates):
        if gate is None:
            payloads.append(
                {
                    "check_id": str(chk.check_id or ""),
                    "endpoint_a": str(chk.endpoint_a),
                    "endpoint_b": str(chk.endpoint_b),
                    "top": top,
                    "status": "fallback",
                    "path_walk_action": "full_path_walk",
                    "use_grep_fast_path": False,
                    "scoped_files": [],
                    "rows": [],
                    "endpoints": [],
                    "log_line": "hgrep-gate missing; full path-walk",
                }
            )
        else:
            payloads.append(check_gate_to_handoff(gate, chk, top=top))
    return write_pathwalk_handoff(path, top=top, checks=payloads, extra=extra)


def seed_path_walk_from_handoff(
    state: Any,
    handoff_check: Mapping[str, Any],
    *,
    all_sources: Sequence[str] = (),
) -> Tuple[FlatRow, ...]:
    """
    Apply one handoff check to *state* when action is seed_and_text_coi.

    Returns seeded folded rows (empty for reject / full_path_walk without seed).
    """
    action = str(handoff_check.get("path_walk_action", "") or "")
    if action != "seed_and_text_coi":
        return ()
    gate = gate_from_handoff_check(handoff_check)
    if not gate.rows:
        return ()
    scoped = scoped_sources_for_gate(gate, all_sources or gate.scoped_files)
    return seed_gate_inst_chain(state, gate, scoped_sources=scoped)


def flat_rows_from_handoff_check(
    handoff_check: Mapping[str, Any],
) -> Tuple[FlatRow, ...]:
    return tuple(flat_row_from_dict(r) for r in (handoff_check.get("rows") or []))


def connectivity_check_from_handoff(
    handoff_check: Mapping[str, Any],
) -> ConnectivityCheck:
    return ConnectivityCheck(
        str(handoff_check.get("endpoint_a", "")),
        str(handoff_check.get("endpoint_b", "")),
        check_id=str(handoff_check.get("check_id", "")),
    )
