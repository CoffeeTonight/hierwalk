# pyhirewalk

Hierarchy-to-hierarchy RTL connectivity / COI knowledge graph.

**Location:** lives under `hierwalk/pyhirewalk` for convenience only. It is a **separate package** — no imports from the parent `hierwalk` tree.

## Design stance

Built **greenfield**. From the older `hierwalk` experiment we take **only** proven EDA filelist semantics (`-f` / `-F`, `+incdir+`, `+define+`, provenance). We do **not** import hierwalk’s path-walk, connect, cache, or scan stack.

| Taken (clean rewrite) | Left behind |
|----------------------|-------------|
| VCS-style `.f` expand | path-walk / zigzag / conn walk |
| Flat slang-safe filelist | full-chip text netlist heuristics |
| Compile context + hash | ignorePath / suite / stress generators |
| Source provenance (which `.f`) | hch DB / hgpath / hgconn |

## Filelist → context

```text
design.f  (+ index_cwd, env)
    → expand (-f/-F semantics)
    → CompileContext { sources, incdirs, defines, top, provenance }
    → flat .f for pyslang (no nested -f/-F)
```

```bash
# install editable
pip install -e ".[dev]"

# expand a filelist
python -m pyhirewalk filelist path/to/design.f --cwd path/to/run_dir
```

## Design

See [DESIGN.md](DESIGN.md) for the full architecture: bundles, zigzag engine, generate/ifdef, cache policy, and phases.

## Run config JSON (recommended at work)

EDA sims take many `+define+` / paths; stuffing the CLI does not scale. Put them in a **run JSON** (JSONC: `//` comments allowed), same idea as company tool configs / hierwalk input JSON — **standalone**, no hierwalk dependency.

```jsonc
{
  "filelist": "$PROJ/rtl/filelist.f",
  "top": "chip_top",
  "cwd": "$PROJ/sim",          // -F index directory
  // Variables used inside .f lines ($PROJ, ${RTL_ROOT}/…) and path fields
  "env": {
    "PROJ": "/proj/chip",
    "RTL_ROOT": "/proj/chip/rtl"
  },
  "defines": {
    "SYNTHESIS": "1",
    "TECH_TSMC": "1",
    "FIFO_DEPTH": "32"
  },
  "build_db": {
    "output": "work/essential.sqlite",
    "work_dir": "work"
  }
}
```

`env` is applied to the process (like hierwalk) and used for `$VAR` / `${VAR}` in filelist contents and in config paths.

### How to run (plain `python3 file.py` — no `-m`)

From the repo root `~/Desktop/hierwalk/pyhirewalk` (or pass absolute paths):

```bash
# DB build from company run JSON
python3 build_db.py --config examples/minimal_bundle/run_build_db.json
python3 run.py examples/minimal_bundle/run_build_db.json

# Or filelist + flags
python3 build_db.py design.f --cwd . --top chip_top -o out.sqlite --define SYNTHESIS

# Expand filelist only
python3 filelist.py --config run.json
python3 filelist.py design.f --cwd . --json
```

Each script adds `src/` to `sys.path` itself — **no install, no `PYTHONPATH`, no `python -m`**.

```bash
pip install pyslang   # needed for build_db.py
```

Example config: `examples/minimal_bundle/run_build_db.json`

## Build essential DB (company timing)

Index **files + module→file only** (no ports, no hierarchy). Measures wall time per phase.

```bash
python -m pyhirewalk build-db /path/to/design.f \
  --cwd /path/to/eda_run_dir \
  --top chip_top \
  --define SYNTHESIS \
  -o /tmp/chip_index.sqlite

python -m pyhirewalk build-db --config run.json --json
```

Python API:

```python
from pyhirewalk import build_essential_db, load_run_config

cfg = load_run_config("run.json")
result = build_essential_db(
    cfg.filelist,
    cfg.db_path,
    index_cwd=cfg.index_cwd,
    top=cfg.top,
    extra_defines=cfg.defines,
    work_dir=cfg.work_dir,
)
print(result.summary())
```

Phases timed: `filelist_expand`, `write_flat_f`, `pyslang_definitions`, `sqlite_write`, `total`.

## Status

Phase 0–1: filelist + essential DB builder. Next: lazy ports, thin hierarchy, zigzag `relate`.
