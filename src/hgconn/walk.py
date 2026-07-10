"""Coarse bloom connectivity using hgpath tree handoff."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from hierwalk.connect.shared.request import ConnectivityCheck

from hgconn.bloom import BloomProbe, bloom_connect
from hgconn.scoped import load_scoped_files
from hgpath.handoff import scoped_files_from_entry
from hgpath.tree_db import TreeEntry

LogFn = Optional[Callable[[str], None]]


@dataclass
class ConnResult:
    check_id: str
    connected: bool
    mode: str
    detail: str
    elapsed_ms: float


def _leaf_name(entry: TreeEntry) -> str:
    if entry.port_tail:
        return entry.port_tail
    if entry.nodes:
        return entry.nodes[-1].segment
    return ""


def check_bloom(
    chk: ConnectivityCheck,
    entry_a: TreeEntry,
    entry_b: TreeEntry,
    *,
    file_bodies: Dict[str, str],
    on_log: LogFn = None,
) -> ConnResult:
    t0 = time.perf_counter()
    if not entry_a.ok or not entry_b.ok:
        ms = (time.perf_counter() - t0) * 1000.0
        return ConnResult(
            check_id=str(chk.check_id or ""),
            connected=False,
            mode="hierarchy-miss",
            detail="hgpath resolve failed",
            elapsed_ms=ms,
        )

    name_a = _leaf_name(entry_a)
    name_b = _leaf_name(entry_b)
    scoped = set(scoped_files_from_entry(entry_a)) | set(scoped_files_from_entry(entry_b))

    best = BloomProbe(False, "miss", "no scoped file hit")
    for fpath in sorted(scoped):
        body = file_bodies.get(fpath, "")
        if not body.strip():
            continue
        probe = bloom_connect(
            body,
            name_a=name_a,
            name_b=name_b,
            file_label=Path_label(fpath),
            on_log=on_log,
        )
        if probe.connected:
            best = probe
            break
        if probe.mode == "miss":
            best = probe

    ms = (time.perf_counter() - t0) * 1000.0
    if on_log:
        on_log(
            f"check-done id={chk.check_id or '-'} connected={best.connected} "
            f"mode=bloom:{best.mode} elapsed_ms={ms:.1f}"
        )
    return ConnResult(
        check_id=str(chk.check_id or ""),
        connected=best.connected,
        mode=best.mode,
        detail=best.detail,
        elapsed_ms=ms,
    )


def Path_label(path: str) -> str:
    from pathlib import Path

    return Path(path).name


def run_bloom_batch(
    check_results: Sequence[Tuple[ConnectivityCheck, TreeEntry, TreeEntry]],
    *,
    on_log: LogFn = None,
) -> List[ConnResult]:
    all_files: List[str] = []
    for _chk, ea, eb in check_results:
        all_files.extend(scoped_files_from_entry(ea))
        all_files.extend(scoped_files_from_entry(eb))
    bodies = load_scoped_files(all_files, on_log=on_log)
    out: List[ConnResult] = []
    for chk, ea, eb in check_results:
        out.append(
            check_bloom(chk, ea, eb, file_bodies=bodies, on_log=on_log)
        )
    return out