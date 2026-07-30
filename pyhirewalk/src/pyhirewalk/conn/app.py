"""HierConnApp: run_conn_check a/b structural connectivity."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyhirewalk.conn.search import ConnSearch, Endpoint
from pyhirewalk.run_config import load_hier_conn_inputs


_TOOL = "hier_conn"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, t0: float) -> None:
    print(
        f"[{_TOOL}] [{_ts()}] (+{time.perf_counter() - t0:8.3f}s) {msg}",
        file=sys.stderr,
    )


def _ensure_hier_resolve_importable() -> None:
    """hier_resolve.py lives at project root (sibling of src/)."""
    # conn/app.py -> conn -> pyhirewalk -> src -> project root
    root = Path(__file__).resolve().parents[3]
    if (root / "hier_resolve.py").is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


class HierConnApp:
    def __init__(
        self,
        *,
        config: Path,
        map_path: Path,
        out: Optional[Path] = None,
        resolve_json: Optional[Path] = None,
        max_hops: int = 64,
    ) -> None:
        self.config = Path(config)
        self.map_path = Path(map_path)
        self.out = Path(out) if out else None
        self.resolve_json = Path(resolve_json) if resolve_json else None
        self.max_hops = max_hops

    def run(self) -> int:
        t0 = time.perf_counter()
        _log(f"hier_conn START  config={self.config}", t0)

        if self.resolve_json is None or not self.resolve_json.is_file():
            _log(
                "ERROR: --resolve hier_resolve.json is required "
                "(COI seeds = resolve ok only)",
                t0,
            )
            return 2

        checks, defines, _cfg_map = load_hier_conn_inputs(self.config)
        _log(f"  n_checks={len(checks)}  n_defines={len(defines)}", t0)
        if not checks:
            _log("ERROR: no run_conn_check.checks in config", t0)
            return 2

        _ensure_hier_resolve_importable()
        from hier_resolve import ModuleMap  # noqa: WPS433

        mmap = ModuleMap.load(self.map_path)
        _log(f"  modules_in_map={len(mmap.modules)}  map={self.map_path}", t0)

        # Seeds ONLY from hier_resolve results (no re-resolve).
        resolve_by_path: Dict[str, Dict[str, Any]] = {}
        doc_r = json.loads(self.resolve_json.read_text(encoding="utf-8"))
        for r in doc_r.get("results") or []:
            if r.get("path"):
                resolve_by_path[str(r["path"])] = r
        _log(
            f"  resolve_json={self.resolve_json}  n_results={len(resolve_by_path)}",
            t0,
        )

        module_files = {k: list(v) for k, v in mmap.modules.items()}
        search = ConnSearch(
            defines=defines,
            module_files=module_files,
            max_hops=self.max_hops,
            log=lambda m: _log(m, t0),
        )

        out_checks: List[Dict[str, Any]] = []
        total_pairs = 0

        for ch in checks:
            cid = ch["id"]
            _log(f"check START id={cid} |a|={len(ch['a'])} |b|={len(ch['b'])}", t0)
            a_ends: List[Endpoint] = []
            b_ends: List[Endpoint] = []
            miss_a: List[str] = []
            miss_b: List[str] = []

            for p in ch["a"]:
                ep = self._endpoint_from_resolve(p, resolve_by_path, search, t0)
                if ep is None:
                    miss_a.append(p)
                else:
                    a_ends.append(ep)
                    _log(
                        f"  a ok {p} -> {ep.module}.{ep.name} "
                        f"fan={ep.fan} port_dir={ep.port_dir} @ {ep.file}",
                        t0,
                    )
            for p in ch["b"]:
                ep = self._endpoint_from_resolve(p, resolve_by_path, search, t0)
                if ep is None:
                    miss_b.append(p)
                else:
                    b_ends.append(ep)
                    _log(
                        f"  b ok {p} -> {ep.module}.{ep.name} "
                        f"fan={ep.fan} port_dir={ep.port_dir} @ {ep.file}",
                        t0,
                    )

            sr = search.run_check(cid, a_ends, b_ends)
            for p in miss_a:
                sr.unconnected.append(
                    {"src": p, "dst": None, "reason": "resolve_miss"}
                )
            for p in miss_b:
                sr.unconnected.append(
                    {"src": None, "dst": p, "reason": "resolve_miss"}
                )

            total_pairs += len(sr.pairs)
            _log(
                f"check END id={cid} pairs={len(sr.pairs)} "
                f"unconnected={len(sr.unconnected)} "
                f"visited_a={sr.stats.get('visited_a')} "
                f"visited_b={sr.stats.get('visited_b')}",
                t0,
            )
            for pr in sr.pairs:
                _log(f"  meet src={pr['src']} dst={pr['dst']} "
                     f"evidence_n={len(pr.get('evidence') or [])}", t0)

            out_checks.append(
                {
                    "id": cid,
                    "pairs": sr.pairs,
                    "unconnected": sr.unconnected,
                    "orphans": [],
                    "cuts": sr.cuts,
                    "stats": sr.stats,
                }
            )

        total = time.perf_counter() - t0
        doc = {
            "schema_version": 1,
            "meta": {
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "config": str(self.config.resolve()),
                "module_map": str(self.map_path.resolve()),
                "resolve": str(self.resolve_json.resolve()),
                "defines": sorted(defines.keys()),
                "stats": {
                    "n_checks": len(checks),
                    "n_pairs": total_pairs,
                    "total_sec": round(total, 6),
                },
            },
            "checks": out_checks,
        }
        text = json.dumps(doc, indent=2) + "\n"
        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(text, encoding="utf-8")
            _log(f"wrote {self.out}", t0)
        else:
            sys.stdout.write(text)

        _log(f"TOTAL_HIER_CONN_SEC={total:.3f}", t0)
        _log("hier_conn END", t0)
        print(f"TOTAL_HIER_CONN_SEC: {total:.3f}", file=sys.stderr)
        return 0

    def _endpoint_from_resolve(
        self,
        path: str,
        resolve_by_path: Dict[str, Dict[str, Any]],
        search: ConnSearch,
        t0: float,
    ) -> Optional[Endpoint]:
        """Seed only if present in hier_resolve results with ok / ok_needs_detail."""
        r = resolve_by_path.get(path)
        if r is None:
            _log(f"  resolve ABSENT {path} (not in --resolve results)", t0)
            return None
        st = r.get("status")
        if st == "miss":
            _log(f"  resolve MISS {path}  {r.get('miss')}", t0)
            return None
        if st not in ("ok", "ok_needs_detail"):
            _log(f"  resolve SKIP {path} status={st}", t0)
            return None

        # instance chain from resolve nodes only
        nodes = r.get("nodes") or []
        for i in range(1, len(nodes)):
            parent = nodes[i - 1]
            child = nodes[i]
            pf = parent.get("file")
            cf = child.get("file")
            inst = child.get("base")
            if pf and cf and inst and child.get("role") != "top":
                search.register_instance(pf, inst, cf)

        leaf = r.get("leaf") or {}
        file = leaf.get("file")
        module = leaf.get("module")
        name = leaf.get("name")
        if not file or not name:
            for n in reversed(nodes):
                if n.get("file") and n.get("status") == "ok":
                    file = file or n.get("file")
                    module = module or n.get("module")
                    break
            if not name:
                name = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if not file or not name:
            return None
        base = name.split("[", 1)[0]
        return Endpoint(
            path=path,
            file=str(file),
            module=str(module or ""),
            name=base,
            port_dir=leaf.get("port_dir"),
            fan=leaf.get("fan"),
        )

    @classmethod
    def main(cls, argv: Optional[List[str]] = None) -> int:
        ap = argparse.ArgumentParser(
            description="Structural COI connectivity between run_conn_check a/b groups"
        )
        ap.add_argument(
            "--config",
            "-c",
            type=Path,
            required=True,
            help="run JSON (run_conn_check.checks a/b, defines, env, modules_json)",
        )
        ap.add_argument(
            "--map",
            "-m",
            type=Path,
            default=None,
            help="modules JSON (priority over config modules_json)",
        )
        ap.add_argument(
            "--resolve",
            type=Path,
            required=True,
            help="hier_resolve.json (required; COI seeds = ok/ok_needs_detail only)",
        )
        ap.add_argument("-o", "--out", type=Path, help="write result JSON")
        ap.add_argument("--max-hops", type=int, default=64)
        args = ap.parse_args(argv)

        map_path = args.map
        if map_path is None:
            _checks, _defs, cfg_map = load_hier_conn_inputs(args.config)
            map_path = cfg_map
        if map_path is None:
            ap.error("give --map / -m (or config modules_json)")

        return cls(
            config=args.config,
            map_path=map_path,
            out=args.out,
            resolve_json=args.resolve,
            max_hops=args.max_hops,
        ).run()


def main(argv: Optional[List[str]] = None) -> int:
    return HierConnApp.main(argv)
