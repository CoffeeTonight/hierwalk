"""Shared run JSON loading for hgpath / hgconn (hier-walk env parity)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.run_request import (
    RUN_CONN_CHECK,
    _mapping_get_ci,
    _parse_defines,
    _resolve_path,
    apply_config_env_from_document,
)


@dataclass
class HgRunConfig:
    filelist: str = ""
    top: str = ""
    index_cwd: Optional[str] = None
    defines: Dict[str, str] = field(default_factory=dict)
    checks: List[ConnectivityCheck] = field(default_factory=list)
    env_applied: List[str] = field(default_factory=list)


def load_json_document(path: Path) -> tuple[Any, Path]:
    base = path.expanduser().resolve().parent
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return raw, base


def _checks_items(doc: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    direct = _mapping_get_ci(doc, "checks")
    if isinstance(direct, list):
        return [c for c in direct if isinstance(c, Mapping)]
    conn = _mapping_get_ci(doc, RUN_CONN_CHECK)
    if isinstance(conn, Mapping):
        nested = _mapping_get_ci(conn, "checks")
        if isinstance(nested, list):
            return [c for c in nested if isinstance(c, Mapping)]
    return []


def parse_checks(doc: Any, *, top: str = "") -> List[ConnectivityCheck]:
    if isinstance(doc, list):
        items = [c for c in doc if isinstance(c, Mapping)]
    elif isinstance(doc, Mapping):
        items = _checks_items(doc)
    else:
        raise SystemExit("checks JSON must be list or object with checks[]")
    out: List[ConnectivityCheck] = []
    for i, item in enumerate(items):
        out.append(
            ConnectivityCheck(
                str(item.get("a", item.get("endpoint_a", ""))),
                str(item.get("b", item.get("endpoint_b", ""))),
                check_id=str(item.get("id", item.get("check_id", f"hg{i}"))),
            )
        )
    return out


def parse_hg_run_config(
    doc: Any,
    base_dir: Path,
    *,
    filelist_cli: str = "",
    top_cli: str = "",
    index_cwd_cli: str = "",
    apply_env: bool = True,
) -> HgRunConfig:
    env_applied: List[str] = []
    if isinstance(doc, Mapping) and apply_env:
        env_applied = apply_config_env_from_document(doc, overwrite=True)

    filelist = str(filelist_cli or "").strip()
    top = str(top_cli or "").strip()
    index_cwd: Optional[str] = str(index_cwd_cli or "").strip() or None
    defines: Dict[str, str] = {}

    if isinstance(doc, Mapping):
        if not filelist:
            fl_raw = str(_mapping_get_ci(doc, "filelist") or "").strip()
            if fl_raw:
                filelist = _resolve_path(base_dir, fl_raw) or ""
        if not top:
            top = str(_mapping_get_ci(doc, "top") or "").strip()
        if not index_cwd:
            cwd_raw = _mapping_get_ci(doc, "index_cwd") or _mapping_get_ci(doc, "index-cwd")
            if cwd_raw:
                index_cwd = _resolve_path(base_dir, str(cwd_raw).strip())
        if _mapping_get_ci(doc, "defines") is not None:
            defines = _parse_defines(_mapping_get_ci(doc, "defines"))

    checks = parse_checks(doc, top=top) if doc is not None else []

    return HgRunConfig(
        filelist=filelist,
        top=top,
        index_cwd=index_cwd,
        defines=defines,
        checks=checks,
        env_applied=env_applied,
    )


def load_hg_run_config(
    checks_path: Path,
    *,
    filelist_cli: str = "",
    top_cli: str = "",
    index_cwd_cli: str = "",
) -> HgRunConfig:
    doc, base = load_json_document(checks_path)
    return parse_hg_run_config(
        doc,
        base,
        filelist_cli=filelist_cli,
        top_cli=top_cli,
        index_cwd_cli=index_cwd_cli,
    )


def require_hg_run_config(
    cfg: HgRunConfig,
    *,
    need_filelist: bool = True,
) -> HgRunConfig:
    if need_filelist and not cfg.filelist:
        raise SystemExit("missing filelist: pass on CLI or in JSON 'filelist'")
    if not cfg.top:
        raise SystemExit("missing top: pass --top or set JSON 'top'")
    return cfg