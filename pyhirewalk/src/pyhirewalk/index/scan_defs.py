"""
Fast module/interface/package name → file scan (no full SV compile).

Company-scale essential DB: scanning 10k+ RTL with pyslang parseAllSources
often takes tens of minutes. For *name → file* catalog only, a line-oriented
scan is enough and typically finishes in well under a minute on similar sets.

Generate (`for` / `if` / `case`) is **mandatory in company RTL**, but it creates
*instances*, not new module *definition files*. This scanner only answers
“which file defines module M?”. Live generate hierarchy is built later by
**scoped pyslang elaborate** (see docs/generate_and_index.md).

Trade-off vs full-chip pyslang at build_db:
  + much faster catalog
  - may see names inside inactive `ifdef branches
  - does not emit g[i] instance paths (correct place: elaborate phase)
  - best-effort on exotic macros that invent keywords
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from pyhirewalk.filelist.paths import path_to_posix

OnProgress = Callable[[str], None]

# module / macromodule / interface / package / program
_DEF_RE = re.compile(
    r"(?m)^\s*(module|macromodule|interface|package|program)\s+"
    r"([A-Za-z_]\w*)\b"
)

# Strip // comments (not perfect inside strings — OK for index)
_LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


def scan_file_definitions(path: Path) -> List[Tuple[str, str, str]]:
    """
    Returns list of (name, kind, abs_path) found in one file.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    text = _strip_comments(raw)
    abs_p = path_to_posix(path)
    out: List[Tuple[str, str, str]] = []
    for m in _DEF_RE.finditer(text):
        kind = m.group(1).lower()
        if kind == "macromodule":
            kind = "module"
        name = m.group(2)
        out.append((name, kind, abs_p))
    return out


def collect_definitions_fast(
    sources: Sequence[Path],
    *,
    on_progress: Optional[OnProgress] = None,
    workers: int = 8,
    progress_every: int = 500,
) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    Scan all source paths for definition names.

    Returns (rows, errors).
    """
    paths = [Path(p) for p in sources if p]
    n = len(paths)
    if on_progress:
        on_progress(
            f"fast-scan: {n} RTL files, workers={workers} "
            f"(no pyslang full parse)"
        )
    if n == 0:
        return [], []

    rows: List[Tuple[str, str, str]] = []
    errors: List[str] = []
    done = 0
    workers = max(1, min(workers, n))

    def _one(p: Path) -> Tuple[Path, List[Tuple[str, str, str]], Optional[str]]:
        try:
            return p, scan_file_definitions(p), None
        except Exception as e:  # noqa: BLE001
            return p, [], str(e)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, p): p for p in paths}
        for fut in as_completed(futs):
            p, found, err = fut.result()
            done += 1
            if err:
                errors.append(f"fast-scan {p}: {err}")
            rows.extend(found)
            if on_progress and (
                done % progress_every == 0 or done == n
            ):
                on_progress(
                    f"fast-scan: {done}/{n} files  defs_so_far={len(rows)}"
                )

    if on_progress:
        on_progress(f"fast-scan: done  files={n} definitions={len(rows)}")
    return rows, errors
