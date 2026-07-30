#!/usr/bin/env python3
"""
Resolve hierarchy paths using build_db modules JSON (modulename → filepath[]).

  python3 hier_resolve.py --map essential.modules.json --path top.u_m.u_l.o
  python3 hier_resolve.py --map m.json --list paths.txt -o out.json
  python3 hier_resolve.py --map m.json top.a.b.sig top.a.b.c.sig

Last segment = signal; middle = instances (or generate label). [index] stripped
for match; needs_detail flagged. Common prefixes share file/instance cache.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
_SEG = re.compile(r"[^.]+")
_IDX = re.compile(r"^([A-Za-z_]\w*)((?:\[[^\]]+\])*)$")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_KW = frozenset(
    "if for case while return assign typedef always always_ff always_comb "
    "always_latch initial final generate end endmodule endinterface endpackage "
    "begin endfunction endtask endgenerate else elseif endcase".split()
)
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
_GEN_LABEL = re.compile(
    r"\bbegin\s*:\s*([A-Za-z_]\w*)\b|\bend\s*:\s*([A-Za-z_]\w*)\b",
    re.I,
)
def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(f"[{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}", file=sys.stderr)


def split_path(h: str) -> List[str]:
    return _SEG.findall(h.strip().strip("."))


def base_sel(seg: str) -> Tuple[str, Optional[str]]:
    m = _IDX.match(seg.strip())
    if not m:
        return seg, None
    sel = m.group(2) or None
    return m.group(1), sel if sel else None


def strip_comments(t: str) -> str:
    # Single-pass state machine (order of // vs /* must not matter).
    try:
        from pyhirewalk.util_comments import strip_sv_comments
    except ImportError:
        _src = Path(__file__).resolve().parent / "src"
        if _src.is_dir() and str(_src) not in sys.path:
            sys.path.insert(0, str(_src))
        from pyhirewalk.util_comments import strip_sv_comments
    return strip_sv_comments(t)


# Preprocessor conditionals — evaluate with provided defines; do NOT blindly strip.
# Line structure preserved (inactive / directive lines → blank) for later file:line.
_PP_LINE = re.compile(
    r"^([ \t]*)`[ \t]*"
    r"(ifdef|ifndef|elsif|elseif|else|endif)\b"
    r"[ \t]*([A-Za-z_]\w*)?"
    r"[^\n]*"
    r"(\n?)$",
    re.M | re.I,
)


def normalize_defines(defines: Optional[Any]) -> Dict[str, str]:
    """Accept dict, list of 'NAME'/'NAME=val', or set of names → name→value map."""
    out: Dict[str, str] = {}
    if defines is None:
        return out
    if isinstance(defines, dict):
        for k, v in defines.items():
            if k is None:
                continue
            name = str(k).strip()
            if not name:
                continue
            out[name] = "" if v is None else str(v)
        return out
    if isinstance(defines, (set, frozenset, list, tuple)):
        for item in defines:
            if item is None:
                continue
            s = str(item).strip()
            if not s:
                continue
            if "=" in s:
                n, _, v = s.partition("=")
                n = n.strip()
                if n:
                    out[n] = v.strip()
            else:
                out[s] = ""
        return out
    return out


def is_defined(name: str, defines: Dict[str, str]) -> bool:
    return name in defines


def apply_sv_ifdefs(text: str, defines: Optional[Any] = None) -> str:
    """
    Evaluate nested `ifdef / `ifndef / `elsif / `else / `endif using *defines*.

    - `ifdef  NAME`  → keep body if NAME is in defines
    - `ifndef NAME`  → keep body if NAME is *not* in defines
    - Inactive branches and the directive lines themselves become blank lines
      (same number of newlines) so line numbers stay aligned with the source file.

    Does **not** expand `define / `include; only conditional inclusion.
    Caller should pass compile/run defines (CLI, run JSON, map meta) later.
    """
    defs = normalize_defines(defines)
    # stack frame: parent_emit, this_emit, taken (some branch of this if-chain already true)
    stack: List[Tuple[bool, bool, bool]] = [(True, True, False)]
    out: List[str] = []

    # split keeping line ends
    parts = re.split(r"(\n)", text)
    # reassemble into lines with optional \n
    lines: List[str] = []
    buf = ""
    for p in parts:
        if p == "\n":
            lines.append(buf + "\n")
            buf = ""
        else:
            buf += p
    if buf:
        lines.append(buf)

    for line in lines:
        m = _PP_LINE.match(line)
        if not m:
            _parent, emit, _taken = stack[-1]
            out.append(line if emit else ("\n" if line.endswith("\n") else ""))
            continue

        kw = m.group(2).lower()
        name = m.group(3) or ""
        # blank out directive line (preserve newline)
        blank = "\n" if line.endswith("\n") else ""

        if kw in ("ifdef", "ifndef"):
            parent_emit = stack[-1][1]
            if not name:
                # malformed — treat as false condition
                cond = False
            elif kw == "ifdef":
                cond = is_defined(name, defs)
            else:
                cond = not is_defined(name, defs)
            this_emit = parent_emit and cond
            stack.append((parent_emit, this_emit, cond))
            out.append(blank)
        elif kw in ("elsif", "elseif"):
            if len(stack) <= 1:
                out.append(blank)
                continue
            parent_emit, _cur, taken = stack[-1]
            if not name:
                cond = False
            else:
                cond = is_defined(name, defs)
            # elsif only if no prior branch taken
            this_emit = parent_emit and (not taken) and cond
            stack[-1] = (parent_emit, this_emit, taken or cond)
            out.append(blank)
        elif kw == "else":
            if len(stack) <= 1:
                out.append(blank)
                continue
            parent_emit, _cur, taken = stack[-1]
            this_emit = parent_emit and (not taken)
            stack[-1] = (parent_emit, this_emit, True)
            out.append(blank)
        elif kw == "endif":
            if len(stack) > 1:
                stack.pop()
            out.append(blank)
        else:
            out.append(blank)

    return "".join(out)


def skip_bal(s: str, i: int, op: str = "(", cl: str = ")") -> int:
    if i >= len(s) or s[i] != op:
        return -1
    d = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == op:
            d += 1
        elif c == cl:
            d -= 1
            if d == 0:
                return i + 1
        i += 1
    return -1


def skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def skip_attr(s: str, i: int) -> int:
    # (* ... *)
    while True:
        i = skip_ws(s, i)
        if i + 1 < len(s) and s[i : i + 2] == "(*":
            j = s.find("*)", i + 2)
            if j < 0:
                return i
            i = j + 2
            continue
        return i


@dataclass
class ModuleMap:
    path: Path
    modules: Dict[str, List[str]]
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ModuleMap":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        mods = doc.get("modules") or {}
        # allow flat {name: path} or {name: [paths]}
        norm: Dict[str, List[str]] = {}
        for k, v in mods.items():
            if isinstance(v, list):
                norm[k] = [str(x) for x in v]
            else:
                norm[k] = [str(v)]
        return cls(Path(path), norm, dict(doc.get("meta") or {}))

    def files(self, name: str) -> List[str]:
        return self.modules.get(name, [])


@dataclass
class HierResolver:
    mmap: ModuleMap
    defines: Dict[str, str] = field(default_factory=dict)
    _body: Dict[str, str] = field(default_factory=dict)
    _inst: Dict[str, Dict[str, str]] = field(default_factory=dict)  # file→base→type
    _gen: Dict[str, set] = field(default_factory=dict)
    _sig: Dict[str, set] = field(default_factory=dict)
    files_opened: int = 0

    def body(self, fp: str) -> str:
        """Comment-stripped + `ifdef-evaluated text (line numbers preserved)."""
        if fp not in self._body:
            self.files_opened += 1
            try:
                raw = Path(fp).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                self._body[fp] = ""
                return self._body[fp]
            # comments first (so // `ifdef in comments is gone), then conditionals
            self._body[fp] = apply_sv_ifdefs(strip_comments(raw), self.defines)
        return self._body[fp]

    def inst_map(self, fp: str) -> Dict[str, str]:
        """instance base name → module type (type must be in map)."""
        if fp in self._inst:
            return self._inst[fp]
        s = self.body(fp)
        out: Dict[str, str] = {}
        i, n = 0, len(s)
        while i < n:
            i = skip_attr(s, i)
            m = _IDENT.match(s, i)
            if not m:
                i += 1
                continue
            typ = m.group(0)
            j = m.end()
            j = skip_ws(s, j)
            if j < n and s[j] == "#":
                j = skip_ws(s, j + 1)
                if j < n and s[j] == "(":
                    j = skip_bal(s, j)
                    if j < 0:
                        break
                j = skip_ws(s, j)
            m2 = _IDENT.match(s, j)
            if not m2:
                i = m.end()
                continue
            inst = m2.group(0)
            k = m2.end()
            k = skip_ws(s, k)
            # array dims
            while k < n and s[k] == "[":
                k = skip_bal(s, k, "[", "]")
                if k < 0:
                    break
                k = skip_ws(s, k)
            if k < n and s[k] in "(;,":
                if typ not in _KW and inst not in _KW and typ in self.mmap.modules:
                    out.setdefault(inst, typ)
            i = m.end()
        self._inst[fp] = out
        return out

    def gen_labels(self, fp: str) -> set:
        if fp not in self._gen:
            s = self.body(fp)
            labs = set()
            for a, b in _GEN_LABEL.findall(s):
                if a:
                    labs.add(a)
                if b:
                    labs.add(b)
            self._gen[fp] = labs
        return self._gen[fp]

    def sigs(self, fp: str) -> set:
        if fp not in self._sig:
            s = self.body(fp)
            names = set(_PORT.findall(s)) | set(_NET.findall(s)) | set(_ASG.findall(s))
            # ANSI: input logic clk, d, e  (names after first, comma-separated)
            for m in re.finditer(
                r"\b(?:input|output|inout)\b[^;()\n]*",
                s,
            ):
                chunk = m.group(0)
                # drop keywords/types/ranges, keep idents
                for idm in _IDENT.finditer(chunk):
                    w = idm.group(0)
                    if w not in (
                        "input",
                        "output",
                        "inout",
                        "wire",
                        "reg",
                        "logic",
                        "bit",
                        "signed",
                        "unsigned",
                        "var",
                        "ref",
                    ):
                        names.add(w)
            self._sig[fp] = names
        return self._sig[fp]

    def resolve_one(self, path: str) -> Dict[str, Any]:
        segs = split_path(path)
        if len(segs) < 2:
            return {
                "path": path,
                "status": "miss",
                "leaf": None,
                "nodes": [],
                "miss": {"reason": "need_at_least_top_and_leaf", "at_index": 0},
            }
        *mids, leaf_raw = segs
        nodes: List[Dict[str, Any]] = []
        needs_any = False

        top_raw = mids[0]
        top_b, top_sel = base_sel(top_raw)
        tfiles = self.mmap.files(top_b)
        if not tfiles:
            return {
                "path": path,
                "status": "miss",
                "leaf": None,
                "nodes": [
                    {
                        "index": 0,
                        "raw": top_raw,
                        "base": top_b,
                        "select": top_sel,
                        "role": "top",
                        "status": "miss",
                        "module": None,
                        "file": None,
                        "needs_detail": bool(top_sel),
                    }
                ],
                "miss": {
                    "at_index": 0,
                    "segment": top_raw,
                    "reason": "unknown_top_module",
                    "searched_in": None,
                    "parent_module": None,
                },
            }
        cur_mod, cur_file = top_b, tfiles[0]
        nd = bool(top_sel)
        needs_any |= nd
        nodes.append(
            {
                "index": 0,
                "raw": top_raw,
                "base": top_b,
                "select": top_sel,
                "role": "top",
                "status": "ok",
                "module": cur_mod,
                "file": cur_file,
                "found_in_file": cur_file,
                "needs_detail": nd,
                "detail_reason": "indexed_segment" if nd else None,
            }
        )

        for idx, raw in enumerate(mids[1:], start=1):
            base, sel = base_sel(raw)
            nd = bool(sel)
            needs_any |= nd
            amap = self.inst_map(cur_file)
            if base in amap:
                child = amap[base]
                cfiles = self.mmap.files(child)
                if not cfiles:
                    nodes.append(
                        {
                            "index": idx,
                            "raw": raw,
                            "base": base,
                            "select": sel,
                            "role": "instance_or_gen",
                            "status": "miss",
                            "module": child,
                            "file": None,
                            "found_in_file": cur_file,
                            "needs_detail": nd,
                        }
                    )
                    return {
                        "path": path,
                        "status": "miss",
                        "leaf": None,
                        "nodes": nodes,
                        "miss": {
                            "at_index": idx,
                            "segment": raw,
                            "reason": "type_not_in_map",
                            "searched_in": cur_file,
                            "parent_module": cur_mod,
                        },
                    }
                nodes.append(
                    {
                        "index": idx,
                        "raw": raw,
                        "base": base,
                        "select": sel,
                        "role": "instance_or_gen",
                        "status": "ok",
                        "module": child,
                        "file": cfiles[0],
                        "found_in_file": cur_file,
                        "bind": {"type": child, "inst_base": base},
                        "needs_detail": nd,
                        "detail_reason": "indexed_segment" if nd else None,
                    }
                )
                cur_mod, cur_file = child, cfiles[0]
                continue
            # generate / named block: stay in same file
            if base in self.gen_labels(cur_file):
                nodes.append(
                    {
                        "index": idx,
                        "raw": raw,
                        "base": base,
                        "select": sel,
                        "role": "instance_or_gen",
                        "status": "ok",
                        "module": cur_mod,
                        "file": cur_file,
                        "found_in_file": cur_file,
                        "bind": {"type": None, "gen_label": base},
                        "needs_detail": True,
                        "detail_reason": "generate_or_named_block",
                    }
                )
                needs_any = True
                continue
            nodes.append(
                {
                    "index": idx,
                    "raw": raw,
                    "base": base,
                    "select": sel,
                    "role": "instance_or_gen",
                    "status": "miss",
                    "module": None,
                    "file": None,
                    "found_in_file": cur_file,
                    "needs_detail": nd,
                }
            )
            return {
                "path": path,
                "status": "miss",
                "leaf": None,
                "nodes": nodes,
                "miss": {
                    "at_index": idx,
                    "segment": raw,
                    "reason": "no_instance_or_gen_label",
                    "searched_in": cur_file,
                    "parent_module": cur_mod,
                },
            }

        # leaf signal
        lb, lsel = base_sel(leaf_raw)
        nd = bool(lsel)
        needs_any |= nd
        li = len(nodes)
        if lb in self.sigs(cur_file):
            leaf = {
                "name": lb,
                "raw": leaf_raw,
                "kind": "signal",
                "select": lsel,
                "found": True,
                "file": cur_file,
                "module": cur_mod,
            }
            nodes.append(
                {
                    "index": li,
                    "raw": leaf_raw,
                    "base": lb,
                    "select": lsel,
                    "role": "signal",
                    "status": "ok",
                    "module": cur_mod,
                    "file": cur_file,
                    "found_in_file": cur_file,
                    "needs_detail": nd,
                    "detail_reason": "signal_select" if nd else None,
                }
            )
            st = "ok_needs_detail" if needs_any else "ok"
            return {
                "path": path,
                "status": st,
                "leaf": leaf,
                "nodes": nodes,
                "miss": None,
            }
        nodes.append(
            {
                "index": li,
                "raw": leaf_raw,
                "base": lb,
                "select": lsel,
                "role": "signal",
                "status": "miss",
                "module": cur_mod,
                "file": cur_file,
                "found_in_file": cur_file,
                "needs_detail": nd,
            }
        )
        return {
            "path": path,
            "status": "miss",
            "leaf": None,
            "nodes": nodes,
            "miss": {
                "at_index": li,
                "segment": leaf_raw,
                "reason": "no_signal",
                "searched_in": cur_file,
                "parent_module": cur_mod,
            },
        }

    def resolve_many(self, paths: List[str]) -> List[Dict[str, Any]]:
        uniq, seen = [], set()
        for p in paths:
            p = p.strip()
            if not p or p.startswith("#") or p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        uniq.sort(key=lambda s: (s.count("."), s))
        return [self.resolve_one(p) for p in uniq]


class HierResolveApp:
    """CLI + orchestration for hierarchy path resolve."""

    def __init__(
        self,
        map_path: Path,
        paths: List[str],
        *,
        out: Optional[Path] = None,
        report_md: Optional[Path] = None,
        fail_any: bool = False,
        defines: Optional[Dict[str, str]] = None,
    ) -> None:
        self.map_path = Path(map_path)
        self.paths = paths
        self.out = Path(out) if out else None
        # default: <out>.miss.md or hier_resolve.miss.md next to map
        if report_md is not None:
            self.report_md = Path(report_md)
        elif self.out is not None:
            self.report_md = self.out.with_suffix(".miss.md")
        else:
            self.report_md = self.map_path.parent / "hier_resolve.miss.md"
        self.fail_any = fail_any
        self.defines = normalize_defines(defines)
        self.doc: Optional[Dict[str, Any]] = None
        self.total_sec: float = 0.0

    @staticmethod
    def format_miss_report(doc: Dict[str, Any]) -> str:
        """Short markdown: full-run summary + miss-only list."""
        meta = doc.get("meta") or {}
        stats = meta.get("stats") or {}
        mmap = meta.get("module_map") or {}
        results = doc.get("results") or []
        misses = [r for r in results if r.get("status") == "miss"]
        needs = [r for r in results if r.get("status") == "ok_needs_detail"]
        n_paths = int(stats.get("n_paths") or len(results))
        n_ok = int(stats.get("n_ok") or 0)
        n_det = int(stats.get("n_ok_needs_detail") or 0)
        n_miss = int(stats.get("n_miss") or len(misses))
        total_sec = stats.get("total_sec")
        files_opened = stats.get("files_opened")

        lines: List[str] = []
        lines.append("# Hierarchy resolve — miss report")
        lines.append("")
        lines.append(f"- **Created:** {meta.get('created_at', '')}")
        lines.append(f"- **Module map:** `{mmap.get('path', '')}`")
        if mmap.get("context_id"):
            lines.append(f"- **context_id:** `{mmap.get('context_id')}`")
        lines.append("")
        lines.append("## Summary (all paths)")
        lines.append("")
        lines.append("| Metric | Count |")
        lines.append("|--------|------:|")
        lines.append(f"| Total paths | {n_paths} |")
        lines.append(f"| OK | {n_ok} |")
        lines.append(f"| OK needs detail (`[]` / gen) | {n_det} |")
        lines.append(f"| **MISS** | **{n_miss}** |")
        if files_opened is not None:
            lines.append(f"| RTL files opened | {files_opened} |")
        if total_sec is not None:
            lines.append(f"| Wall time (sec) | {total_sec} |")
        hit = (n_ok + n_det) / n_paths * 100.0 if n_paths else 0.0
        lines.append(f"| Hit rate (ok+detail) | {hit:.1f}% |")
        lines.append("")

        # miss reason breakdown
        reason_cnt: Dict[str, int] = {}
        for r in misses:
            reason = (r.get("miss") or {}).get("reason") or "unknown"
            reason_cnt[reason] = reason_cnt.get(reason, 0) + 1
        if reason_cnt:
            lines.append("### Miss by reason")
            lines.append("")
            lines.append("| Reason | Count |")
            lines.append("|--------|------:|")
            for k, v in sorted(reason_cnt.items(), key=lambda x: (-x[1], x[0])):
                lines.append(f"| `{k}` | {v} |")
            lines.append("")

        lines.append("## Miss hierarchies only")
        lines.append("")
        if not misses:
            lines.append("_No misses._")
            lines.append("")
        else:
            lines.append("| # | Path | Fail segment | Reason | Searched in | Parent module |")
            lines.append("|--:|------|--------------|--------|-------------|---------------|")
            for i, r in enumerate(misses, 1):
                m = r.get("miss") or {}
                path = r.get("path", "")
                seg = m.get("segment") or ""
                reason = m.get("reason") or ""
                searched = m.get("searched_in") or ""
                parent = m.get("parent_module") or ""
                # shorten paths for readability
                if searched:
                    searched = f"`{searched}`"
                lines.append(
                    f"| {i} | `{path}` | `{seg}` | `{reason}` | {searched} | `{parent}` |"
                )
            lines.append("")
            lines.append("### Miss path list (copy-friendly)")
            lines.append("")
            lines.append("```")
            for r in misses:
                lines.append(r.get("path", ""))
            lines.append("```")
            lines.append("")

        if needs:
            lines.append("## OK but needs_detail (not miss)")
            lines.append("")
            lines.append("| Path | Indexed / gen segments |")
            lines.append("|------|------------------------|")
            for r in needs:
                flags = [
                    n.get("raw")
                    for n in (r.get("nodes") or [])
                    if n.get("needs_detail")
                ]
                lines.append(
                    f"| `{r.get('path', '')}` | {', '.join(f'`{x}`' for x in flags) or '—'} |"
                )
            lines.append("")

        lines.append("---")
        lines.append(
            "_Full detail: companion JSON report "
            f"(`schema_version` {doc.get('schema_version', 1)})._"
        )
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def load_path_list(
        path_args: Optional[List[str]] = None,
        list_file: Optional[Path] = None,
        positional: Optional[List[str]] = None,
    ) -> List[str]:
        out: List[str] = list(path_args or []) + list(positional or [])
        if list_file:
            out.extend(
                ln.strip()
                for ln in Path(list_file).read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            )
        return out

    def run(self) -> int:
        t0 = time.perf_counter()
        _log(f"hier_resolve START  map={self.map_path}", t0)
        _log(f"  n_paths_in={len(self.paths)}", t0)

        mmap = ModuleMap.load(self.map_path)
        # defines: CLI/app > map meta.defines / meta.defines
        map_defs = normalize_defines(
            mmap.meta.get("defines") or mmap.meta.get("define")
        )
        defs = {**map_defs, **self.defines}
        _log(
            f"  modules_in_map={len(mmap.modules)}  "
            f"context_id={mmap.meta.get('context_id')}  "
            f"defines={len(defs)}",
            t0,
        )
        if defs:
            sample = ", ".join(list(defs.keys())[:8])
            more = "…" if len(defs) > 8 else ""
            _log(f"  define_names=[{sample}{more}]", t0)

        resolver = HierResolver(mmap, defines=defs)
        results = resolver.resolve_many(self.paths)

        n_ok = sum(1 for r in results if r["status"] in ("ok", "ok_needs_detail"))
        n_miss = sum(1 for r in results if r["status"] == "miss")
        n_det = sum(1 for r in results if r["status"] == "ok_needs_detail")
        total = time.perf_counter() - t0
        self.total_sec = total

        self.doc = {
            "schema_version": 1,
            "meta": {
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "module_map": {
                    "path": str(self.map_path.resolve()),
                    "format": "json",
                    "context_id": mmap.meta.get("context_id"),
                },
                "options": {
                    "strip_index_for_match": True,
                    "comment_strip": True,
                    "ifdef_eval": True,
                    "defines": sorted(defs.keys()),
                    "detail_policy": "flag_only",
                },
                "stats": {
                    "n_paths": len(results),
                    "n_ok": n_ok - n_det,
                    "n_ok_needs_detail": n_det,
                    "n_miss": n_miss,
                    "files_opened": resolver.files_opened,
                    "total_sec": round(total, 6),
                },
            },
            "results": results,
        }

        text = json.dumps(self.doc, indent=2) + "\n"
        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(text, encoding="utf-8")
            _log(f"wrote {self.out}", t0)
        else:
            sys.stdout.write(text)

        # Short MD: full summary + miss-only section
        md = self.format_miss_report(self.doc)
        self.report_md.parent.mkdir(parents=True, exist_ok=True)
        self.report_md.write_text(md, encoding="utf-8")
        _log(f"wrote miss report {self.report_md}", t0)

        for r in results:
            st = r["status"]
            flag = {"ok": "OK ", "ok_needs_detail": "OK*", "miss": "MISS"}.get(st, st)
            extra = ""
            if r.get("miss"):
                extra = (
                    f"  # {r['miss'].get('reason')} @ {r['miss'].get('segment')}"
                )
            elif r.get("leaf"):
                extra = (
                    f"  # {r['leaf'].get('module')}.{r['leaf'].get('name')} "
                    f"→ {r['leaf'].get('file')}"
                )
            print(f"{flag}  {r['path']}{extra}", file=sys.stderr)

        _log(
            f"summary ok={n_ok}/{len(results)} miss={n_miss} "
            f"needs_detail={n_det} files_opened={resolver.files_opened}",
            t0,
        )
        _log(
            f"TOTAL_HIER_RESOLVE_SEC={total:.3f}  ({total / 60.0:.3f} min)",
            t0,
        )
        _log("hier_resolve END", t0)
        print(f"TOTAL_HIER_RESOLVE_SEC: {total:.3f}", file=sys.stderr)

        if self.fail_any and n_miss:
            return 1
        return 0

    @classmethod
    def main(cls, argv: Optional[List[str]] = None) -> int:
        ap = argparse.ArgumentParser(
            description="Hierarchy resolve → JSON (module map)"
        )
        ap.add_argument(
            "--map",
            "-m",
            type=Path,
            default=None,
            help="modules JSON from build_db (*.modules.json); "
            "optional if --config sets modules_json",
        )
        ap.add_argument(
            "--path", "-p", action="append", default=[], help="hierarchy path"
        )
        ap.add_argument("--list", "-l", type=Path, help="paths file, one per line")
        ap.add_argument("paths", nargs="*", help="extra paths")
        ap.add_argument("-o", "--out", type=Path, help="write result JSON")
        ap.add_argument(
            "--report-md",
            type=Path,
            default=None,
            help="miss summary markdown (default: <out>.miss.md or "
            "hier_resolve.miss.md next to --map)",
        )
        ap.add_argument(
            "--fail-any", action="store_true", help="exit 1 if any miss"
        )
        ap.add_argument(
            "-D",
            "--define",
            action="append",
            default=[],
            metavar="NAME[=VAL]",
            help="preprocessor define for `ifdef/`ifndef (repeatable). "
            "Also read map meta.defines / --config defines.",
        )
        ap.add_argument(
            "--config",
            "-c",
            type=Path,
            default=None,
            help="run JSON: run_conn_check.checks[].a/b hierarchies; "
            "defines for `ifdef; env for $VAR in modules_json path; "
            "modules_json for map. "
            "Does not use filelist/hier_resolve.paths as hierarchy input.",
        )
        args = ap.parse_args(argv)

        # --config: ONLY checks[*].a and checks[*].b → path list for resolve_many.
        # Do not load_run_config (filelist/env/paths noise).
        cfg_defines: Dict[str, str] = {}
        cfg_paths: List[str] = []
        map_path = args.map
        if args.config is not None:
            try:
                from pyhirewalk.run_config import load_hier_resolve_inputs
            except ImportError:
                _src = Path(__file__).resolve().parent / "src"
                if _src.is_dir() and str(_src) not in sys.path:
                    sys.path.insert(0, str(_src))
                from pyhirewalk.run_config import load_hier_resolve_inputs
            cfg_paths, cfg_defines, cfg_map = load_hier_resolve_inputs(args.config)
            if map_path is None and cfg_map is not None:
                map_path = cfg_map
            _log(
                f"config={args.config}: "
                f"n_hier_from_checks_a_b={len(cfg_paths)} "
                f"n_defines={len(cfg_defines)}",
                time.perf_counter(),
            )

        if map_path is None:
            ap.error("give --map / -m (or --config with modules_json)")

        # Explicit CLI paths optional; config contributes ONLY check a/b.
        paths = cls.load_path_list(args.path, args.list, args.paths)
        paths = list(paths) + list(cfg_paths)
        seen: set[str] = set()
        uniq: List[str] = []
        for p in paths:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                uniq.append(p)
        paths = uniq
        if not paths:
            ap.error(
                "no hierarchies: set run_conn_check.checks[].a and .b in --config "
                "(or --path / --list)"
            )

        defs = {**cfg_defines, **normalize_defines(args.define)}
        return cls(
            map_path,
            paths,
            out=args.out,
            report_md=args.report_md,
            fail_any=args.fail_any,
            defines=defs,
        ).run()


def main(argv: Optional[List[str]] = None) -> int:
    return HierResolveApp.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
