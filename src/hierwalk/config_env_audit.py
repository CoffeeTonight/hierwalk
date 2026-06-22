"""Log JSON ``env`` blocks and effective hier-walk behavioral environment."""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Mapping, Optional, Sequence, TextIO

from hierwalk.lazy_scope import lazy_index_ifdef, lazy_processing_enabled
from hierwalk.perf import (
    body_param_scan_max,
    include_warm_enabled,
    log_large_module_skips,
    low_memory_auto_threshold,
    pw_db_prefetch_enabled,
    pw_db_prefetch_max_files,
    pw_db_prefetch_wait_on_exit,
    slow_file_log_threshold_sec,
)
from hierwalk.preprocess import _define_active
from hierwalk.run_request import _config_env_block, _mapping_get_ci, _parse_defines

# Env vars that affect index/preprocess/connect behavior (stable order for logs).
_BEHAVIOR_ENV_VARS: Sequence[tuple[str, str, str]] = (
    (
        "HIERWALK_LAZY",
        "1",
        "master lazy switch (1=defer ifdef/macro at index; 0=eager full index+ifdef)",
    ),
    (
        "HIERWALK_LAZY_IFDEF",
        "0",
        "when lazy=1: apply Verilog ifdef at index (1=strip inactive ifndef branches)",
    ),
    (
        "HIERWALK_JOBS",
        "(unset)",
        "worker count override (unset=auto); also settable via JSON jobs",
    ),
    (
        "HIERWALK_CACHE_DIR",
        "(unset)",
        "override work/cache root (unset=.db_{TOP} under index-cwd or cwd)",
    ),
    (
        "HIERWALK_IGNORE_PATH",
        "(unset)",
        "default --ignore-path globs (comma-separated)",
    ),
    (
        "HIERWALK_IGNORE_MODULE",
        "(unset)",
        "default --ignore-module names (comma-separated)",
    ),
    (
        "HIERWALK_IGNORE_FILELIST",
        "(unset)",
        "default --ignore-filelist patterns (comma-separated)",
    ),
    (
        "HIERWALK_INCLUDE_WARM",
        "0",
        "opt-in shared-include warm before parallel index (1=enable)",
    ),
    (
        "HIERWALK_NO_INCLUDE_WARM",
        "0",
        "skip include discovery/warm entirely (1=skip)",
    ),
    (
        "HIERWALK_INCLUDE_WARM_MAX",
        "200",
        "max includes to warm (0=no limit)",
    ),
    (
        "HIERWALK_PW_DB_BUILD",
        "off",
        "post-verify full path-walk DB: off | after_verify",
    ),
    (
        "HIERWALK_PW_DB_PREFETCH",
        "0",
        "legacy alias: 1 => after_verify full DB build",
    ),
    (
        "HIERWALK_PW_DB_PREFETCH_WAIT",
        "1",
        "wait for tier-1 prefetch thread before returning (0=detach)",
    ),
    (
        "HIERWALK_PW_DB_PREFETCH_MAX",
        "0",
        "cap tier-1 prefetch files per run (0=no limit)",
    ),
    (
        "HIERWALK_LOG_SLOW_FILES",
        "(unset)",
        "log slow per-file index timing (1=10s threshold, or seconds)",
    ),
    (
        "HIERWALK_LOW_MEMORY_AUTO",
        "1500",
        "auto fused low-memory index above N sources (0=off)",
    ),
    (
        "HIERWALK_BODY_PARAM_SCAN_MAX",
        "524288",
        "max module body bytes for param scan at index (0=always)",
    ),
    (
        "HIERWALK_LOG_LARGE_MODULES",
        "0",
        "stderr when large modules skip body param collection",
    ),
    (
        "HCH_INDEX_CWD",
        "(unset)",
        "EDA cwd for -F nested filelists (filelist index cwd)",
    ),
)


def _env_source(name: str, json_env_keys: Iterable[str]) -> str:
    json_set = {str(k).strip() for k in json_env_keys}
    if name in json_set:
        return "json:env"
    if name in os.environ:
        return "shell"
    return "default"


def _effective_env_display(name: str, raw: Optional[str], default: str) -> str:
    if raw is None or raw == "":
        if default.startswith("("):
            return default
        return default
    return raw


def _index_ifdef_policy_line() -> str:
    if not lazy_processing_enabled():
        return (
            "active-at-index (HIERWALK_LAZY=0 eager mode; "
            "ifndef branches filtered per defines)"
        )
    if lazy_index_ifdef():
        return (
            "active-at-index (HIERWALK_LAZY_IFDEF=1; "
            "inactive ifndef/ifdef branches removed at index)"
        )
    return (
        "deferred (ifdef not applied at index; all ifndef branches kept in index — "
        "filelist +define+ and JSON defines do not strip ifndef here)"
    )


def _json_defines_lines(document: Optional[Mapping[str, Any]]) -> List[str]:
    if document is None:
        return ["config-env: verilog-defines from JSON: (no run JSON document)"]
    raw = _mapping_get_ci(document, "defines")
    if raw is None:
        return [
            "config-env: verilog-defines from JSON top-level: (none — "
            "filelist +define+ may still add macros at filelist parse)"
        ]
    try:
        parsed = _parse_defines(raw)
    except ValueError as exc:
        return [f"config-env: verilog-defines from JSON top-level: ERROR ({exc})"]
    if not parsed:
        return [
            "config-env: verilog-defines from JSON top-level: (empty object/array)"
        ]
    parts = [f"{k}={v}" for k, v in sorted(parsed.items())]
    return [f"config-env: verilog-defines from JSON top-level: {'; '.join(parts)}"]


def _json_env_block_lines(
    document: Optional[Mapping[str, Any]],
    json_env_applied: Sequence[str],
) -> List[str]:
    if document is None:
        return []
    block = _config_env_block(document)
    if block is None:
        return ["config-env: JSON env block: (none)"]
    declared = [
        f"{str(k).strip()}={'' if v is None else str(v).strip()}"
        for k, v in block.items()
        if str(k).strip()
    ]
    if not declared:
        return ["config-env: JSON env block: (empty object)"]
    lines = [
        "config-env: JSON env block declared "
        f"({len(declared)}): {'; '.join(declared)}"
    ]
    if json_env_applied:
        lines.append(
            "config-env: JSON env applied to process "
            f"({len(json_env_applied)}): {'; '.join(sorted(set(json_env_applied)))}"
        )
    return lines


def format_config_env_audit_lines(
    document: Optional[Mapping[str, Any]] = None,
    *,
    json_env_applied: Optional[Sequence[str]] = None,
) -> List[str]:
    """
    Human-readable audit of JSON env + effective behavioral environment.

    Call after :func:`hierwalk.run_request.apply_config_env_from_document`.
    """
    applied = list(json_env_applied or ())
    lines: List[str] = [
        "config-env: === run environment (after JSON env, before filelist parse) ===",
    ]
    lines.extend(_json_env_block_lines(document, applied))
    lines.extend(_json_defines_lines(document))
    lines.append(f"config-env: index-ifdef-policy: {_index_ifdef_policy_line()}")

    slow = slow_file_log_threshold_sec()
    lines.append(
        "config-env: derived: "
        f"lazy_processing={int(lazy_processing_enabled())} "
        f"lazy_index_ifdef={int(lazy_index_ifdef())} "
        f"include_warm={int(include_warm_enabled())} "
        f"low_memory_auto_threshold={low_memory_auto_threshold()} "
        f"body_param_scan_max={body_param_scan_max()} "
        f"log_large_modules={int(log_large_module_skips())} "
        f"slow_file_log_sec={slow if slow is not None else 'off'}"
    )

    lines.append("config-env: behavioral HIERWALK_* / HCH_INDEX_CWD (effective):")
    for name, default, note in _BEHAVIOR_ENV_VARS:
        raw = os.environ.get(name)
        source = _env_source(name, applied)
        effective = _effective_env_display(name, raw, default)
        lines.append(f"config-env:   {name}={effective} source={source} — {note}")

    other_json = sorted(
        {
            str(k).strip()
            for k in applied
            if str(k).strip()
            and not any(str(k).strip() == spec[0] for spec in _BEHAVIOR_ENV_VARS)
        }
    )
    if other_json:
        parts = []
        for key in other_json:
            val = os.environ.get(key, "")
            parts.append(f"{key}={val}")
        lines.append(
            "config-env: other JSON env keys (non-behavioral): " + "; ".join(parts)
        )

    lines.append(
        "config-env: note: full effective RTL macros logged after filelist parse "
        "(run: verilog-defines: ...)"
    )
    return lines


def format_verilog_defines_audit_lines(
    *,
    effective_defines: Mapping[str, str],
    json_defines: Optional[Mapping[str, str]] = None,
    connect_defines: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Log merged RTL macros used for preprocess/ifdef after filelist parse."""
    json_map = dict(json_defines or {})
    connect_map = dict(connect_defines or {})
    lines: List[str] = [
        "verilog-defines: effective RTL macros (JSON + filelist +define+ + connect)",
    ]

    if not effective_defines:
        lines.append(
            "verilog-defines: (none — in-file `define may still apply per file during preprocess)"
        )
        return lines

    filelist_only = {
        k: v
        for k, v in effective_defines.items()
        if k not in json_map and k not in connect_map
    }
    if json_map:
        parts = [f"{k}={v!r}" for k, v in sorted(json_map.items())]
        lines.append(f"verilog-defines: from JSON defines ({len(json_map)}): {'; '.join(parts)}")
    if connect_map:
        parts = [f"{k}={v!r}" for k, v in sorted(connect_map.items())]
        lines.append(
            f"verilog-defines: from connect batch ({len(connect_map)}): {'; '.join(parts)}"
        )
    if filelist_only:
        parts = [f"{k}={v!r}" for k, v in sorted(filelist_only.items())]
        lines.append(
            f"verilog-defines: from filelist +define+ only ({len(filelist_only)}): "
            f"{'; '.join(parts)}"
        )

    lines.append(f"verilog-defines: merged ({len(effective_defines)}):")
    for name in sorted(effective_defines):
        val = effective_defines[name]
        active = _define_active(name, effective_defines)
        sources: List[str] = []
        if name in json_map:
            sources.append("json")
        if name in connect_map:
            sources.append("connect")
        if name in filelist_only:
            sources.append("filelist")
        src = "+".join(sources) if sources else "merged"
        lines.append(
            f"verilog-defines:   {name}={val!r} source={src} active={int(active)}"
        )
    return lines


def emit_verilog_defines_audit(
    *,
    effective_defines: Mapping[str, str],
    json_defines: Optional[Mapping[str, str]] = None,
    connect_defines: Optional[Mapping[str, str]] = None,
    stream: Optional[TextIO] = None,
) -> None:
    import sys

    out = stream if stream is not None else sys.stderr
    for line in format_verilog_defines_audit_lines(
        effective_defines=effective_defines,
        json_defines=json_defines,
        connect_defines=connect_defines,
    ):
        print(f"run: {line}", file=out, flush=True)


def emit_config_env_audit(
    document: Optional[Mapping[str, Any]] = None,
    *,
    json_env_applied: Optional[Sequence[str]] = None,
    stream: Optional[TextIO] = None,
) -> None:
    import sys

    out = stream if stream is not None else sys.stderr
    for line in format_config_env_audit_lines(
        document,
        json_env_applied=json_env_applied,
    ):
        print(f"run: {line}", file=out, flush=True)