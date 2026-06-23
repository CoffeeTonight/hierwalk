"""Trace stop policies for cone / inst-trace (hierarchy glob + max depth)."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from hierwalk.models import FlatRow

_INT_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class TraceStopPolicy:
    ignore_hierarchy: Tuple[str, ...] = ()
    trace_max_depth: Optional[int] = None


def _parse_depth_value(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{field} depth must be a non-negative integer, not boolean")
    if isinstance(raw, int):
        depth = raw
    else:
        text = str(raw).strip()
        if not _INT_RE.match(text):
            raise ValueError(f"{field} depth must be a non-negative integer, got {raw!r}")
        depth = int(text)
    if depth < 0:
        raise ValueError(f"{field} depth must be >= 0, got {depth}")
    return depth


def parse_trace_stop_policy(
    data: Optional[Mapping[str, Any]] = None,
    *,
    ignore_hierarchy: Any = None,
    trace_max_depth: Any = None,
) -> TraceStopPolicy:
    """
    Parse trace stop settings from a mapping and/or explicit fields.

    ``ignore_hierarchy`` entries that are plain integers (or numeric strings) set
    ``trace_max_depth`` (minimum wins when multiple numbers appear).
    """
    patterns: list[str] = []
    depth: Optional[int] = None

    if trace_max_depth is not None:
        depth = _parse_depth_value(trace_max_depth, field="trace_max_depth")

    raw_list = ignore_hierarchy
    if raw_list is None and data is not None:
        raw_list = (
            data.get("ignore_hierarchy")
            or data.get("ignore-hierarchy")
        )

    if raw_list is not None:
        if isinstance(raw_list, str):
            items = [raw_list]
        elif isinstance(raw_list, (list, tuple)):
            items = list(raw_list)
        else:
            raise ValueError("ignore_hierarchy must be a string or array")
        for item in items:
            if isinstance(item, int) or (
                isinstance(item, str) and _INT_RE.match(item.strip())
            ):
                n = _parse_depth_value(item, field="ignore_hierarchy")
                depth = n if depth is None else min(depth, n)
                continue
            text = str(item).strip()
            if not text:
                raise ValueError("ignore_hierarchy patterns must be non-empty")
            patterns.append(text)

    if data is not None and trace_max_depth is None:
        raw_depth = data.get("trace_max_depth", data.get("trace-max-depth"))
        if raw_depth is not None:
            depth = _parse_depth_value(raw_depth, field="trace_max_depth")

    return TraceStopPolicy(
        ignore_hierarchy=tuple(patterns),
        trace_max_depth=depth,
    )


def instance_depth(
    scope: str,
    *,
    top: str,
    row: Optional[FlatRow] = None,
) -> int:
    """Hierarchy depth aligned with elaboration (top = 0)."""
    if row is not None:
        return int(row.depth)
    text = (scope or "").strip()
    top_name = (top or "").strip()
    if not text or text == top_name:
        return 0
    if top_name and text.startswith(top_name + "."):
        return text[len(top_name) + 1 :].count(".") + 1
    return max(0, text.count("."))


def hierarchy_ignore_match(scope: str, pattern: str) -> bool:
    """
    Return True when instance *scope* is stopped by *pattern*.

    - ``top.a.b.c.*`` — strict descendants of ``top.a.b.c`` (not ``c`` itself)
    - ``top.a.b.c`` — ``c`` and all descendants
    - globs with ``*`` / ``?`` — ``fnmatch`` on the full dotted path
    """
    pat = pattern.strip()
    inst = (scope or "").strip()
    if not pat or not inst:
        return False
    if pat.endswith(".*"):
        prefix = pat[:-2]
        if not prefix:
            return bool(inst)
        if inst == prefix:
            return False
        return inst.startswith(prefix + ".")
    if any(ch in pat for ch in ("*", "?", "[")):
        return fnmatch.fnmatchcase(inst, pat)
    return inst == pat or inst.startswith(pat + ".")


def scope_ignored_by_hierarchy(
    scope: str,
    patterns: Sequence[str],
) -> Optional[str]:
    for pat in patterns:
        if hierarchy_ignore_match(scope, pat):
            return pat
    return None


def scope_exceeds_trace_depth(
    scope: str,
    *,
    top: str,
    row: Optional[FlatRow],
    trace_max_depth: Optional[int],
) -> bool:
    if trace_max_depth is None:
        return False
    return instance_depth(scope, top=top, row=row) > trace_max_depth


def trace_stop_boundary_kind(
    scope: str,
    *,
    top: str,
    row: Optional[FlatRow],
    policy: TraceStopPolicy,
    is_origin: bool = False,
) -> Optional[Tuple[str, str]]:
    """
    Return (boundary_kind, detail) when *scope* is a trace stop point.

    Origin scope is exempt so tracing can start at deep endpoints.
    """
    if is_origin or not scope:
        return None
    hit = scope_ignored_by_hierarchy(scope, policy.ignore_hierarchy)
    if hit is not None:
        return ("ignore-hierarchy", f"ignore_hierarchy match {hit!r}")
    if scope_exceeds_trace_depth(
        scope,
        top=top,
        row=row,
        trace_max_depth=policy.trace_max_depth,
    ):
        depth = instance_depth(scope, top=top, row=row)
        return (
            "trace-depth",
            f"trace_max_depth={policy.trace_max_depth} (scope depth {depth})",
        )
    return None


def trace_stop_from_fields(
    *,
    ignore_hierarchy: Sequence[str] = (),
    trace_max_depth: Optional[int] = None,
) -> TraceStopPolicy:
    return TraceStopPolicy(
        ignore_hierarchy=tuple(ignore_hierarchy),
        trace_max_depth=trace_max_depth,
    )