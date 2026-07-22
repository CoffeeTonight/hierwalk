"""Shared run JSON loading for hgpath / hgconn (hier-walk env parity)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.run_request import (
    RUN_CONN_CHECK,
    _mapping_get_ci,
    _parse_defines,
    _resolve_path,
    apply_config_env_from_document,
    read_json_document,
)


@dataclass
class HgRunConfig:
    filelist: str = ""
    top: str = ""
    index_cwd: Optional[str] = None
    defines: Dict[str, str] = field(default_factory=dict)
    checks: List[ConnectivityCheck] = field(default_factory=list)
    env_applied: List[str] = field(default_factory=list)
    simple_exist: bool = False


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    text = str(val or "").strip().lower()
    return text in ("1", "true", "yes", "on")


def load_json_document(path: Path) -> tuple[Any, Path]:
    """Load JSON/JSONC (``//`` line comments) — same parser as hier-walk."""
    resolved = path.expanduser().resolve()
    return read_json_document(resolved), resolved.parent


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
    """
    Parse checks[] into ConnectivityCheck rows.

    JSON list endpoints must go through the same expand path as hier-walk
    (``_parse_check_endpoints``). Using ``str(list)`` produces Python repr
    ``"['xa.b.c', 'xa.d.e.r']"`` — not JSON display ``[xa.b.c, xa.d.e.r]`` —
    so LPM logs show quoted specs like ``'xa.b.c'`` / ``instance xa.'xa not found``.
    """
    from hierwalk.connect.shared.request import _parse_check_item

    if isinstance(doc, list):
        items = list(doc)
    elif isinstance(doc, Mapping):
        items = list(_checks_items(doc))
    else:
        raise SystemExit("checks JSON must be list or object with checks[]")
    out: List[ConnectivityCheck] = []
    for i, item in enumerate(items):
        out.append(_parse_check_item(item, index=i))
    return out


def parse_hg_run_config(
    doc: Any,
    base_dir: Path,
    *,
    filelist_cli: str = "",
    top_cli: str = "",
    index_cwd_cli: str = "",
    simple_exist_cli: bool = False,
    apply_env: bool = True,
) -> HgRunConfig:
    env_applied: List[str] = []
    if isinstance(doc, Mapping) and apply_env:
        env_applied = apply_config_env_from_document(doc, overwrite=True)

    filelist = str(filelist_cli or "").strip()
    top = str(top_cli or "").strip()
    index_cwd: Optional[str] = str(index_cwd_cli or "").strip() or None
    defines: Dict[str, str] = {}
    simple_exist = bool(simple_exist_cli)

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
        if not simple_exist_cli:
            raw_se = _mapping_get_ci(doc, "simple_exist")
            if raw_se is None:
                raw_se = _mapping_get_ci(doc, "simple-exist")
            if raw_se is not None:
                simple_exist = _parse_bool(raw_se)

    checks = parse_checks(doc, top=top) if doc is not None else []

    return HgRunConfig(
        filelist=filelist,
        top=top,
        index_cwd=index_cwd,
        defines=defines,
        checks=checks,
        env_applied=env_applied,
        simple_exist=simple_exist,
    )


def load_hg_run_config(
    checks_path: Path,
    *,
    filelist_cli: str = "",
    top_cli: str = "",
    index_cwd_cli: str = "",
    simple_exist_cli: bool = False,
) -> HgRunConfig:
    doc, base = load_json_document(checks_path)
    return parse_hg_run_config(
        doc,
        base,
        filelist_cli=filelist_cli,
        top_cli=top_cli,
        index_cwd_cli=index_cwd_cli,
        simple_exist_cli=simple_exist_cli,
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