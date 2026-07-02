"""Short tagged stderr lines for path-walk preprocessing (``HIERWALK_PP_LOG``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, List, Optional, Union

PathLike = Union[str, Path]

_pp_log_sinks: List[Callable[[str], None]] = []

# Tags users can quote in one or two words (grep: ``[hier-walk pp]``).
PP_MISS = "pp-miss"
PP_DISK = "pp-disk"
PP_MEM = "pp-mem"
PP_CLOSURE = "pp-closure"
PP_T1 = "pp-t1"
PP_T1_HIT = "pp-t1-hit"
PP_T0 = "pp-t0"
PP_T0_HIT = "pp-t0-hit"
PP_DUP = "pp-dup"
PP_SLOW = "pp-slow"
PP_DEFINES = "pp-defines"

_LEVEL1_TAGS = frozenset(
    {
        PP_MISS,
        PP_DISK,
        PP_CLOSURE,
        PP_T1,
        PP_T1_HIT,
        PP_T0,
        PP_T0_HIT,
        PP_DUP,
        PP_SLOW,
        PP_DEFINES,
    }
)
_HIT_ONLY_TAGS = frozenset({PP_MEM})


def _basename(path: PathLike) -> str:
    try:
        return Path(path).name
    except (TypeError, ValueError):
        return str(path)


def register_pp_log_sink(sink: Callable[[str], None]) -> None:
    """Mirror pp lines to a run log (e.g. ``.hier-walk.log``) in addition to stderr."""
    if sink not in _pp_log_sinks:
        _pp_log_sinks.append(sink)


def clear_pp_log_sinks() -> None:
    _pp_log_sinks.clear()


def format_pp_log_line(
    tag: str,
    path: PathLike,
    *,
    ms: Optional[float] = None,
    incl: Optional[int] = None,
    out_mib: Optional[float] = None,
    mods: Optional[int] = None,
    inst: Optional[int] = None,
    names: Optional[int] = None,
    detail: str = "",
) -> Optional[str]:
    """Build one pp log line, or None when ``HIERWALK_PP_LOG`` filters it out."""
    from hierwalk.perf import preprocess_log_level, preprocess_log_slow_ms

    level = preprocess_log_level()
    if level <= 0:
        return None
    if tag in _HIT_ONLY_TAGS and level < 2:
        return None
    if level < 2 and tag not in _LEVEL1_TAGS:
        return None
    slow_ms = preprocess_log_slow_ms()
    if (
        level < 2
        and ms is not None
        and ms < slow_ms
        and tag not in (PP_MISS, PP_T1, PP_T0, PP_DUP, PP_SLOW)
    ):
        if tag != PP_CLOSURE or (incl or 0) < 20:
            return None

    parts = [f"[hier-walk pp] {tag} {_basename(path)}"]
    if ms is not None:
        parts.append(f"{ms:.0f}ms")
    if incl is not None:
        parts.append(f"incl={incl}")
    if out_mib is not None:
        parts.append(f"{out_mib:.1f}MiB")
    if mods is not None:
        parts.append(f"mods={mods}")
    if inst is not None:
        parts.append(f"inst={inst}")
    if names is not None:
        parts.append(f"names={names}")
    if detail:
        parts.append(detail)
    return " ".join(parts)


def emit_pp_log(
    tag: str,
    path: PathLike,
    *,
    ms: Optional[float] = None,
    incl: Optional[int] = None,
    out_mib: Optional[float] = None,
    mods: Optional[int] = None,
    inst: Optional[int] = None,
    names: Optional[int] = None,
    detail: str = "",
) -> None:
    """
    Emit one preprocessing line to stderr when ``HIERWALK_PP_LOG`` allows it.

    Levels (``HIERWALK_PP_LOG``):
      0 / off — disabled
      1 / brief (default) — misses, disk/tier hits, scans, closure, dup, slow
      2 / all — also ``pp-mem`` and fast closure
    """
    line = format_pp_log_line(
        tag,
        path,
        ms=ms,
        incl=incl,
        out_mib=out_mib,
        mods=mods,
        inst=inst,
        names=names,
        detail=detail,
    )
    if line is None:
        return
    print(line, file=sys.stderr, flush=True)
    for sink in _pp_log_sinks:
        try:
            sink(line)
        except OSError:
            pass