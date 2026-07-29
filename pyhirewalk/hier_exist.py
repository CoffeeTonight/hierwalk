#!/usr/bin/env python3
"""
Hierarchy path existence using only build_db SQLite (module → file).

  python3 hier_exist.py --db work/essential.sqlite --path top.a.b.sig
  python3 hier_exist.py --db work/x.sqlite --list paths.txt
  python3 hier_exist.py --db work/x.sqlite top.a.b.c top.a.b.c.d.f

Last segment = signal (port/wire/reg/logic/…). Middle segments = instances.
Shares work across paths with common prefixes (normal in large check lists).
No pyslang — DB lookup + small regex on the few files on each path.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# path parse: top.qwe[0].p0.o[9].rr[4].sig  →  ["top","qwe[0]",...,"sig"]
# ---------------------------------------------------------------------------
_SEG = re.compile(r"[^.]+")
_IDX = re.compile(r"^([A-Za-z_]\w*)((?:\[[^\]]+\])*)$")

# instance:  ModName [#(...)] inst  |  ModName inst (
_INST = re.compile(
    r"(?m)^\s*([A-Za-z_]\w*)\s*"
    r"(?:#\s*\([^;]*?\))?\s*"
    r"([A-Za-z_]\w*)\s*(?:\[|\(|;|,)"
)
# port/net/assign names (best-effort; ANSI + body)
_PORT = re.compile(
    r"\b(?:input|output|inout)\b"
    r"(?:\s+(?:wire|reg|logic|bit|signed|unsigned|var))*"
    r"(?:\s+\[[^\]]+\])*"
    r"\s+([A-Za-z_]\w*)\b"
)
_NET = re.compile(
    r"\b(?:wire|reg|logic|bit|integer|int|tri)\b"
    r"(?:\s+signed|\s+unsigned)?"
    r"(?:\s+\[[^\]]+\])*"
    r"\s+([A-Za-z_]\w*)\b"
)
_ASG = re.compile(r"\bassign\s+([A-Za-z_]\w*)\b")


def split_path(hier: str) -> List[str]:
    h = hier.strip().strip(".")
    if not h:
        return []
    return _SEG.findall(h)


def base_name(seg: str) -> str:
    m = _IDX.match(seg.strip())
    return m.group(1) if m else seg


@dataclass
class PathResult:
    path: str
    ok: bool
    detail: str = ""
    files_opened: int = 0


@dataclass
class HierExistChecker:
    """
    Walk instance chain using modules table; last node = signal in final module.
    Caches: module text, instance map, signal set — reused for shared prefixes.
    """

    db_path: Path
    _mod2files: Dict[str, List[str]] = field(default_factory=dict)
    _text: Dict[str, str] = field(default_factory=dict)  # file → body
    _inst: Dict[str, Dict[str, str]] = field(default_factory=dict)  # file → inst→mod
    _sigs: Dict[str, set] = field(default_factory=dict)  # file → signal names
    _opens: int = 0

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            for name, path in con.execute(
                "SELECT name, f.path FROM modules m JOIN files f ON m.file_id=f.file_id"
            ):
                self._mod2files.setdefault(str(name), []).append(str(path))
        finally:
            con.close()

    def _body(self, path: str) -> str:
        if path not in self._text:
            self._opens += 1
            try:
                self._text[path] = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                self._text[path] = ""
        return self._text[path]

    def _inst_map(self, path: str) -> Dict[str, str]:
        if path not in self._inst:
            m: Dict[str, str] = {}
            for mod, inst in _INST.findall(self._body(path)):
                if mod in ("if", "for", "case", "return", "assign", "typedef"):
                    continue
                m.setdefault(inst, mod)  # first wins
            self._inst[path] = m
        return self._inst[path]

    def _sig_set(self, path: str) -> set:
        if path not in self._sigs:
            body = self._body(path)
            s = set(_PORT.findall(body)) | set(_NET.findall(body)) | set(_ASG.findall(body))
            self._sigs[path] = s
        return self._sigs[path]

    def _files_for_module(self, mod: str) -> List[str]:
        return self._mod2files.get(mod, [])

    def check_one(self, hier: str) -> PathResult:
        segs = split_path(hier)
        if len(segs) < 2:
            return PathResult(hier, False, "need instance…signal (min 2 segments)")
        *inst_chain, leaf = segs
        leaf_b = base_name(leaf)
        # first segment: top module name (usual) or sole instance of that module
        top = base_name(inst_chain[0])
        files = self._files_for_module(top)
        if not files:
            return PathResult(hier, False, f"unknown top/module {top!r} (not in DB)")
        # prefer unique definition file
        cur_mod, cur_file = top, files[0]
        opened0 = self._opens

        for seg in inst_chain[1:]:
            ib = base_name(seg)
            amap = self._inst_map(cur_file)
            child_mod = amap.get(ib)
            if not child_mod:
                return PathResult(
                    hier,
                    False,
                    f"no instance {ib!r} in module {cur_mod} ({Path(cur_file).name})",
                    self._opens - opened0,
                )
            cfiles = self._files_for_module(child_mod)
            if not cfiles:
                return PathResult(
                    hier,
                    False,
                    f"instance {ib} → type {child_mod!r} not in DB",
                    self._opens - opened0,
                )
            cur_mod, cur_file = child_mod, cfiles[0]

        if leaf_b not in self._sig_set(cur_file):
            return PathResult(
                hier,
                False,
                f"no signal {leaf_b!r} in module {cur_mod} ({Path(cur_file).name})",
                self._opens - opened0,
            )
        return PathResult(hier, True, f"{cur_mod}.{leaf_b}", self._opens - opened0)

    def check_many(self, paths: Iterable[str]) -> List[PathResult]:
        """Sort + walk so shared prefixes reuse cached inst/sig maps."""
        uniq = []
        seen = set()
        for p in paths:
            p = p.strip()
            if not p or p.startswith("#") or p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        # deeper paths after parents → warm cache along shared spine
        uniq.sort(key=lambda s: (s.count("."), s))
        return [self.check_one(p) for p in uniq]


def _load_list(path: Path) -> List[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hierarchy path exist check (build_db SQLite)")
    ap.add_argument("--db", "-d", type=Path, required=True, help="essential.sqlite from build_db")
    ap.add_argument("--path", "-p", action="append", default=[], help="hier path (repeatable)")
    ap.add_argument("--list", "-l", type=Path, help="file with one path per line")
    ap.add_argument("paths", nargs="*", help="extra paths")
    ap.add_argument("--fail-any", action="store_true", help="exit 1 if any path missing")
    args = ap.parse_args(argv)

    paths: List[str] = list(args.path) + list(args.paths)
    if args.list:
        paths.extend(_load_list(args.list))
    if not paths:
        ap.error("give --path / --list / positional paths")

    chk = HierExistChecker(args.db)
    results = chk.check_many(paths)
    ok_n = sum(1 for r in results if r.ok)
    for r in results:
        flag = "OK " if r.ok else "MISS"
        print(f"{flag}  {r.path}  # {r.detail}")
    print(
        f"# summary ok={ok_n}/{len(results)}  files_read≈{chk._opens}  db={args.db}",
        file=sys.stderr,
    )
    if args.fail_any and ok_n < len(results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
