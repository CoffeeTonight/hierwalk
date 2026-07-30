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
_LINE_C = re.compile(r"//.*?$", re.M)
_BLOCK_C = re.compile(r"/\*.*?\*/", re.S)


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
    return _LINE_C.sub(" ", _BLOCK_C.sub(" ", t))


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
    _body: Dict[str, str] = field(default_factory=dict)
    _inst: Dict[str, Dict[str, str]] = field(default_factory=dict)  # file→base→type
    _gen: Dict[str, set] = field(default_factory=dict)
    _sig: Dict[str, set] = field(default_factory=dict)
    files_opened: int = 0

    def body(self, fp: str) -> str:
        if fp not in self._body:
            self.files_opened += 1
            try:
                self._body[fp] = strip_comments(
                    Path(fp).read_text(encoding="utf-8", errors="ignore")
                )
            except OSError:
                self._body[fp] = ""
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


def _load_paths(args: argparse.Namespace) -> List[str]:
    out: List[str] = list(args.path or []) + list(args.paths or [])
    if args.list:
        out.extend(
            ln.strip()
            for ln in Path(args.list).read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
    return out


def main(argv: Optional[List[str]] = None) -> int:
    t0 = time.perf_counter()
    ap = argparse.ArgumentParser(description="Hierarchy resolve → JSON (module map)")
    ap.add_argument(
        "--map",
        "-m",
        type=Path,
        required=True,
        help="modules JSON from build_db (*.modules.json)",
    )
    ap.add_argument("--path", "-p", action="append", default=[], help="hierarchy path")
    ap.add_argument("--list", "-l", type=Path, help="paths file, one per line")
    ap.add_argument("paths", nargs="*", help="extra paths")
    ap.add_argument("-o", "--out", type=Path, help="write result JSON")
    ap.add_argument("--fail-any", action="store_true", help="exit 1 if any miss")
    args = ap.parse_args(argv)

    paths = _load_paths(args)
    if not paths:
        ap.error("give --path / --list / positional paths")

    _log(f"hier_resolve START  map={args.map}", t0)
    _log(f"  n_paths_in={len(paths)}", t0)

    mmap = ModuleMap.load(args.map)
    _log(
        f"  modules_in_map={len(mmap.modules)}  context_id={mmap.meta.get('context_id')}",
        t0,
    )

    res = HierResolver(mmap)
    results = res.resolve_many(paths)

    n_ok = sum(1 for r in results if r["status"] in ("ok", "ok_needs_detail"))
    n_miss = sum(1 for r in results if r["status"] == "miss")
    n_det = sum(1 for r in results if r["status"] == "ok_needs_detail")
    total = time.perf_counter() - t0

    doc = {
        "schema_version": 1,
        "meta": {
            "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "module_map": {
                "path": str(Path(args.map).resolve()),
                "format": "json",
                "context_id": mmap.meta.get("context_id"),
            },
            "options": {
                "strip_index_for_match": True,
                "comment_strip": True,
                "detail_policy": "flag_only",
            },
            "stats": {
                "n_paths": len(results),
                "n_ok": n_ok - n_det,
                "n_ok_needs_detail": n_det,
                "n_miss": n_miss,
                "files_opened": res.files_opened,
                "total_sec": round(total, 6),
            },
        },
        "results": results,
    }

    text = json.dumps(doc, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        _log(f"wrote {args.out}", t0)
    else:
        sys.stdout.write(text)

    for r in results:
        st = r["status"]
        flag = {"ok": "OK ", "ok_needs_detail": "OK*", "miss": "MISS"}.get(st, st)
        extra = ""
        if r.get("miss"):
            extra = f"  # {r['miss'].get('reason')} @ {r['miss'].get('segment')}"
        elif r.get("leaf"):
            extra = f"  # {r['leaf'].get('module')}.{r['leaf'].get('name')} → {r['leaf'].get('file')}"
        print(f"{flag}  {r['path']}{extra}", file=sys.stderr)

    _log(
        f"summary ok={n_ok}/{len(results)} miss={n_miss} needs_detail={n_det} "
        f"files_opened={res.files_opened}",
        t0,
    )
    _log(f"TOTAL_HIER_RESOLVE_SEC={total:.3f}  ({total / 60.0:.3f} min)", t0)
    _log("hier_resolve END", t0)
    print(f"TOTAL_HIER_RESOLVE_SEC: {total:.3f}", file=sys.stderr)

    if args.fail_any and n_miss:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
