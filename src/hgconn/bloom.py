"""Bloom-style coarse connectivity: name presence on RHS/LHS (no false negative)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

LogFn = Optional[Callable[[str], None]]

_WORD = re.compile(r"[A-Za-z_]\w*")


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def word_boundary_find(haystack: str, needle: str, start: int = 0) -> int:
    if not needle:
        return -1
    hlow = haystack.lower()
    nlow = needle.lower()
    nlen = len(needle)
    pos = start
    while pos < len(haystack):
        idx = hlow.find(nlow, pos)
        if idx < 0:
            return -1
        before = haystack[idx - 1] if idx > 0 else ""
        after_i = idx + nlen
        after = haystack[after_i] if after_i < len(haystack) else ""
        if not (before and _is_word_char(before)) and not (
            after and _is_word_char(after)
        ):
            return idx
        pos = idx + 1
    return -1


def _strip_comments(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        if "//" in raw:
            raw = raw.split("//", 1)[0]
        lines.append(raw)
    return "\n".join(lines)


def _assign_rhs_has_name(clean: str, name: str) -> bool:
    pos = 0
    while pos < len(clean):
        idx = word_boundary_find(clean, name, pos)
        if idx < 0:
            return False
        window = clean[max(0, idx - 96) : idx].lower()
        if "assign" in window or "<=" in window:
            return True
        pos = idx + 1
    return False


def _assign_lhs_rhs_link(clean: str, lhs: str, rhs: str) -> bool:
    """Loose: same assign statement mentions both names (bloom)."""
    for stmt in clean.split(";"):
        low = stmt.lower()
        if "assign" not in low and "<=" not in low:
            continue
        if word_boundary_find(stmt, lhs) >= 0 and word_boundary_find(stmt, rhs) >= 0:
            return True
    return False


@dataclass
class BloomProbe:
    connected: bool
    mode: str
    detail: str


def bloom_connect(
    text: str,
    *,
    name_a: str,
    name_b: str,
    file_label: str = "",
    on_log: LogFn = None,
) -> BloomProbe:
    """
    Coarse bloom connectivity in one RTL file body.

    Prefer assign co-occurrence; fall back to word presence (no false negative).
    """
    clean = _strip_comments(text)
    lhs_a, lhs_b = name_a.strip(), name_b.strip()
    if not lhs_a or not lhs_b:
        return BloomProbe(False, "miss", "empty name")

    if lhs_a == lhs_b:
        probe = BloomProbe(True, "same-net", f"same name {lhs_a!r}")
        if on_log:
            on_log(
                f"bloom probe file={file_label} lhs={lhs_a!r} rhs={lhs_b!r} "
                f"hit={probe.mode}"
            )
        return probe

    if _assign_lhs_rhs_link(clean, lhs_a, lhs_b):
        probe = BloomProbe(True, "assign", f"assign mentions {lhs_a!r} and {lhs_b!r}")
    elif _assign_rhs_has_name(clean, lhs_b) or _assign_rhs_has_name(clean, lhs_a):
        probe = BloomProbe(True, "assign-one-sided", "assign drive mentions name")
    elif word_boundary_find(clean, lhs_a) >= 0 and word_boundary_find(clean, lhs_b) >= 0:
        probe = BloomProbe(True, "word", f"both names present in {file_label or 'body'}")
    elif word_boundary_find(clean, lhs_b) >= 0 or word_boundary_find(clean, lhs_a) >= 0:
        probe = BloomProbe(True, "word-partial", "one name present (bloom lenient)")
    else:
        probe = BloomProbe(False, "miss", "no assign/word hit")

    if on_log:
        on_log(
            f"bloom probe file={file_label} lhs={lhs_a!r} rhs={lhs_b!r} hit={probe.mode}"
        )
    return probe