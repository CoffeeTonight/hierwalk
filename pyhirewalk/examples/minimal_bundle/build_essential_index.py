#!/usr/bin/env python3
"""Build essential L1/L2 index JSON for this example using pyslang."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "src"))

from pyhirewalk.context import build_context  # noqa: E402


def main() -> int:
    try:
        import pyslang
    except ImportError:
        print("pyslang required: pip install pyslang", file=sys.stderr)
        return 1

    VA = pyslang.ast.VisitAction
    ctx = build_context(ROOT / "filelist.f", index_cwd=ROOT, top="top")
    flat = ROOT / "_flat.slang.f"
    ctx.write_slang_filelist(flat)
    body = flat.read_text()
    if "-top" not in body:
        flat.write_text("-top top\n" + body)

    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.processCommandFiles(str(flat), False, False):
        print("processCommandFiles failed", file=sys.stderr)
        return 1
    if not driver.processOptions() or not driver.parseAllSources():
        print("parse failed", file=sys.stderr)
        return 1
    comp = driver.createCompilation()
    sm = driver.sourceManager

    def file_of(sym) -> str | None:
        loc = getattr(sym, "location", None)
        if not loc:
            return None
        return str(Path(sm.getFileName(loc)).resolve())

    files = [
        {"file_id": i + 1, "path": p.name, "role": "listed"}
        for i, p in enumerate(ctx.source_files)
    ]
    abs_to_id = {
        str(p.resolve()): i + 1 for i, p in enumerate(ctx.source_files)
    }

    modules = []
    for d in comp.getDefinitions():
        fp = file_of(d)
        modules.append(
            {
                "name": d.name,
                "file_id": abs_to_id.get(str(Path(fp).resolve()) if fp else "", None),
                "file": Path(fp).name if fp else None,
            }
        )

    instances: list[dict] = []
    ports_by_type: dict[str, list] = {}

    def add_ports(inst) -> None:
        tname = inst.definition.name
        if tname in ports_by_type:
            return
        ports_by_type[tname] = [
            {
                "name": p.name,
                "dir": str(getattr(p, "direction", "?")).split(".")[-1],
            }
            for p in inst.body.portList
        ]

    def walk_inst(inst, parent_path, origin="plain", gen_index=None) -> None:
        path = inst.hierarchicalPath
        defn = inst.definition
        add_ports(inst)
        fp = file_of(defn)
        instances.append(
            {
                "hier_path": path,
                "type": defn.name,
                "parent": parent_path,
                "origin": origin,
                "gen_index": gen_index,
                "def_file": Path(fp).name if fp else None,
            }
        )
        body_sym = inst.body
        found: list[tuple[str, object]] = []

        def on_node(sym):
            if sym is body_sym:
                return None
            k = str(sym.kind)
            if "GenerateBlockArray" in k:
                found.append(("garr", sym))
                return VA.Skip
            if "GenerateBlock" in k and "Array" not in k:
                found.append(("gblk", sym))
                return VA.Skip
            if "Instance" in k and "Body" not in k and "Array" not in k:
                if getattr(sym, "hierarchicalPath", "") != path:
                    found.append(("inst", sym))
                    return VA.Skip
            return None

        body_sym.visit(on_node)

        for kind, sym in found:
            if kind == "garr":
                for idx, entry in enumerate(sym.entries):

                    def on_e(s, idx=idx):
                        sk = str(s.kind)
                        if "Instance" in sk and "Body" not in sk and "Array" not in sk:
                            walk_inst(
                                s, path, origin="generate_for", gen_index=idx
                            )
                            return VA.Skip
                        return None

                    entry.visit(on_e)
            elif kind == "gblk":

                def on_e(s):
                    sk = str(s.kind)
                    if "Instance" in sk and "Body" not in sk and "Array" not in sk:
                        walk_inst(s, path, origin="generate_if", gen_index=None)
                        return VA.Skip
                    return None

                sym.visit(on_e)
            else:
                walk_inst(sym, path, origin="plain")

    tops = [t for t in comp.getRoot().topInstances if t.name == "top"]
    if not tops:
        print("no top instance", file=sys.stderr)
        return 1
    walk_inst(tops[0], parent_path=None, origin="top")

    db = {
        "meta": {
            "context_id": ctx.context_id,
            "top": "top",
            "defines": dict(ctx.defines),
        },
        "files": files,
        "modules": modules,
        "ports": ports_by_type,
        "instances": instances,
    }
    out = ROOT / "essential_index.example.json"
    out.write_text(json.dumps(db, indent=2) + "\n")
    print(f"wrote {out} ({len(instances)} instances)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
