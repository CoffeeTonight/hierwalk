"""Detect common ``enable`` misconfigurations in flat run JSON."""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Tuple

from hierwalk.run_request import (
    RUN_CONN_CHECK,
    RUN_CONE_TRACE,
    RUN_IO_TRACE,
    RUN_ON_FULL_DB_LEGACY,
    RUN_ON_FULL_INDEX,
    _mapping_get_ci,
    block_enable_raw,
    parse_enable,
)

_NESTED_ENABLE_KEYS = ("settings", "config", "options", "execution", "run")

_COMMENTED_ENABLE_ZERO = re.compile(
    r'//\s*"(?:enable|enabled)"\s*:\s*(?:0|false)\b',
    re.IGNORECASE,
)


def commented_enable_zero_in_block_text(
    raw_text: str,
    block_key: str,
) -> bool:
    """
    True when JSONC line comments hide ``"enable": 0`` inside a suite block.

    ``strip_jsonc_line_comments`` removes whole-line ``//`` comments, so a
    commented-out enable line is treated as missing and defaults to enabled.
    """
    if not raw_text or not block_key:
        return False
    key_pat = re.compile(
        rf'"{re.escape(block_key)}"\s*:\s*\{{',
        re.IGNORECASE,
    )
    match = key_pat.search(raw_text)
    if match is None:
        return False
    start = match.end() - 1
    depth = 0
    end = start
    for i, ch in enumerate(raw_text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block_body = raw_text[start : end + 1]
    return _COMMENTED_ENABLE_ZERO.search(block_body) is not None


def nested_enable_misplacement(
    spec: Mapping[str, Any],
    *,
    block_key: str,
) -> Tuple[Optional[Any], Optional[str]]:
    """Return (raw_enable, warning) when enable sits under a nested object."""
    for nest_key in _NESTED_ENABLE_KEYS:
        sub = _mapping_get_ci(spec, nest_key)
        if not isinstance(sub, Mapping):
            continue
        nested_raw = block_enable_raw(sub)
        if nested_raw is not None:
            return (
                nested_raw,
                f"{block_key}.{nest_key} contains enable/enabled — "
                f"move it to the top of the {block_key} object",
            )
    return None, None


def top_level_enable_raw(document: Mapping[str, Any]) -> Any:
    return block_enable_raw(document)


def verification_blocks_present(document: Mapping[str, Any]) -> bool:
    """True when any run_conn_check / run_io_trace / run_cone_trace block exists."""
    return any(_mapping_get_ci(document, kind) is not None for kind in (
        RUN_CONN_CHECK,
        RUN_IO_TRACE,
        RUN_CONE_TRACE,
    ))


def default_enable_for_suite_block(
    block_key: str,
    document: Optional[Mapping[str, Any]],
    *,
    fallback: bool = True,
) -> bool:
    """
    Default enable when a block omits enable/enabled.

    run_on_full_index defaults to **off** when verification blocks exist so
    per-step enable on run_conn_check / run_io_trace / run_cone_trace does not
    implicitly turn hierarchy back on.
    """
    if document is None:
        return fallback
    if block_key.lower() in (RUN_ON_FULL_INDEX, RUN_ON_FULL_DB_LEGACY):
        if verification_blocks_present(document):
            return False
    return fallback


def find_nested_full_index_blocks(
    data: Any,
    *,
    path: str = "",
    max_depth: int = 5,
) -> List[Tuple[str, Mapping[str, Any]]]:
    """Locate run_on_full_index / run_on_full_db blocks not at document root."""
    if max_depth < 0 or not isinstance(data, Mapping):
        return []
    hits: List[Tuple[str, Mapping[str, Any]]] = []
    for raw_key, value in data.items():
        if not isinstance(raw_key, str) or not isinstance(value, Mapping):
            continue
        key_lower = raw_key.lower()
        subpath = f"{path}.{raw_key}" if path else raw_key
        if key_lower in (RUN_ON_FULL_INDEX, RUN_ON_FULL_DB_LEGACY):
            if path:
                hits.append((subpath, value))
            continue
        if key_lower in (
            "filelist",
            "top",
            "defines",
            "tests",
            "env",
            "environment",
            RUN_CONN_CHECK,
            RUN_IO_TRACE,
            RUN_CONE_TRACE,
            RUN_ON_FULL_INDEX,
            RUN_ON_FULL_DB_LEGACY,
        ):
            continue
        hits.extend(
            find_nested_full_index_blocks(
                value,
                path=subpath,
                max_depth=max_depth - 1,
            )
        )
    return hits


def resolve_block_enabled(
    spec: Mapping[str, Any],
    *,
    default: bool = True,
    document: Optional[Mapping[str, Any]] = None,
    block_key: str = "",
    raw_text: Optional[str] = None,
) -> Tuple[bool, Tuple[str, ...]]:
    """
    Parse block enable with root-cause fixes for common JSON mistakes.

    Returns ``(enabled, warnings)``.
    """
    warnings: List[str] = []
    raw = block_enable_raw(spec)
    if raw is not None:
        return parse_enable(raw, default=default), tuple(warnings)

    nested_raw, nested_warn = nested_enable_misplacement(spec, block_key=block_key)
    if nested_raw is not None:
        if nested_warn:
            warnings.append(nested_warn)
        return parse_enable(nested_raw, default=default), tuple(warnings)

    is_full_index_block = block_key.lower() in (
        RUN_ON_FULL_INDEX,
        RUN_ON_FULL_DB_LEGACY,
    )
    if document is not None and is_full_index_block:
        top_raw = top_level_enable_raw(document)
        if top_raw is not None:
            warnings.append(
                f"top-level enable/enabled applies to {block_key} — "
                f"put \"enable\" inside the {block_key} object"
            )
            return parse_enable(top_raw, default=default), tuple(warnings)

    if raw_text and block_key and commented_enable_zero_in_block_text(
        raw_text, block_key
    ):
        warnings.append(
            f"JSONC comment hid \"enable\": 0 in {block_key} — "
            f"the line was stripped before parse; use an uncommented "
            f"\"enable\": 0 inside {block_key}"
        )
        return False, tuple(warnings)

    effective_default = default_enable_for_suite_block(
        block_key,
        document,
        fallback=default,
    )
    if is_full_index_block and raw is None:
        if effective_default:
            warnings.append(
                f"{block_key} has no enable/enabled field — defaulting to enabled (1); "
                f"set \"enable\": 0 inside {block_key} to skip hierarchy/index"
            )
        else:
            warnings.append(
                f"{block_key} has no enable/enabled field — defaulting to disabled (0) "
                f"because verification blocks are present; set \"enable\": 1 inside "
                f"{block_key} to run hierarchy/index"
            )
    return effective_default, tuple(warnings)


def audit_run_on_full_index_enable_lines(
    document: Mapping[str, Any],
    *,
    raw_text: Optional[str] = None,
) -> Tuple[str, ...]:
    """
    Lines that state exactly what the parser read for run_on_full_index enable.

    Use these stderr lines to prove whether enable: 0 reached the runtime.
    """
    from hierwalk.run_request import _full_index_block_key

    lines: List[str] = []
    key = _full_index_block_key(document)
    if key is None:
        lines.append(
            "enable-audit: run_on_full_index block not found in parsed JSON "
            "(check key spelling: run_on_full_index or run_on_full_db)"
        )
        return tuple(lines)

    spec = _mapping_get_ci(document, key)
    if not isinstance(spec, Mapping):
        lines.append(f"enable-audit: block={key} is not a JSON object")
        return tuple(lines)

    raw_enable = block_enable_raw(spec)
    enabled, _ = resolve_block_enabled(
        spec,
        default=True,
        document=document,
        block_key=key,
        raw_text=raw_text,
    )
    lines.append(
        f"enable-audit: block={key} raw_enable={raw_enable!r} "
        f"parsed_enable={int(enabled)} "
        f"action={'SCHEDULE' if enabled else 'SKIP'}"
    )

    if raw_text:
        dup = _duplicate_enable_assignments_in_block(raw_text, key)
        if dup > 1:
            lines.append(
                f"enable-audit: WARNING {key} contains {dup} 'enable'/'enabled' "
                f"assignments in source text — JSON keeps only the last one"
            )
    return tuple(lines)


def _duplicate_enable_assignments_in_block(raw_text: str, block_key: str) -> int:
    key_pat = re.compile(
        rf'"{re.escape(block_key)}"\s*:\s*\{{',
        re.IGNORECASE,
    )
    match = key_pat.search(raw_text)
    if match is None:
        return 0
    start = match.end() - 1
    depth = 0
    end = start
    for i, ch in enumerate(raw_text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = raw_text[start : end + 1]
    return len(
        re.findall(
            r'"(?:enable|enabled)"\s*:',
            body,
            flags=re.IGNORECASE,
        )
    )


def format_enable_root_cause_hint(
    *,
    saw_suite_trace: bool,
    saw_hierarchy_execute: bool,
    saw_full_index_step: bool,
    version: str,
) -> Optional[str]:
    """One-line hint when stderr matches the user's failure signature."""
    if not saw_hierarchy_execute:
        return None
    if not saw_suite_trace:
        return (
            "ROOT CAUSE: this hier-walk binary did not execute flat-suite parsing "
            f"(no enable-trace: block= lines; version={version}). "
            "enable:0 in JSON is ignored on that code path — reinstall so "
            "hier-walk --version shows this package directory, not an old "
            "site-packages build."
        )
    if saw_full_index_step:
        return (
            "ROOT CAUSE: parsed_enable=1 for run_on_full_index despite your "
            "enable:0 — see enable-audit line above; if raw_enable=0 but "
            "parsed_enable=1, file a bug; if raw_enable=1, duplicate enable "
            "keys or a different value is winning in the parsed JSON."
        )
    return None