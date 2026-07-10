# hgpath + hgconn implementation plan

**Purpose:** Coarse structural connectivity — hierarchy existence (hgpath) then bloom-style text-conn (hgconn).  
**Model:** Middle hops = **inst**; last hop only = **port/wire/reg/assign/logic**.  
**DBs:** Flat (`module→filepath`) + Tree (`path→inst chain + leaf + file per node`). No heavy module-index DB for text-conn.

## Tools

| Tool | Role | Input | Output DB / report |
|------|------|-------|-------------------|
| **hgpath** | Hierarchy exists? | filelist, checks, top | `hgpath_flat.json`, `hgpath_tree.json`, `hgpath.report` |
| **hgconn** | Loose connect (bloom) | hgpath DBs + checks | `hgconn.report` (never false-negative bias) |

## Principles

- Logs: always `[YYYY-MM-DD HH:MM:SS]` prefix.
- Reports: hgpath end + hgconn end; each includes `elapsed_sec` total.
- No heavy deps; small modules; debug lines at cache hit/miss, scoped files, bloom probe.
- Verify: run script + pytest after each step; read timing/logs for anomalies.
- Text-conn: bloom = word/RHS-LHS presence OK; **must not miss real conn**; parse fail → word fallback.
- Regression: zigzag torture parity (`test_hierarchy_grep_zigzag`, `zigzag_torture_gen`).

## Implementation table

| Step | ID | Deliverable | Verify |
|------|-----|-------------|--------|
| 0 | S0 | `IMPLEMENTATION_PLAN.md`, package layout `hgpath/`, `hgconn/`, `hg_core/` | import smoke |
| 1 | S1 | `hg_core/log.py` timestamped stderr+log file | unit test timestamp |
| 2 | S2 | `hg_core/report.py` concise summary + elapsed | unit test |
| 3 | S3 | `hgpath/flat_db.py` wrap `grep_hie` build/load | roundtrip test |
| 4 | S4 | `hgpath/path_norm.py` inst vs leaf split (gate rules) | unit norm |
| 5 | S5 | `hgpath/tree_db.py` LPM trie, node filepath invariant | LPM unit test |
| 6 | S6 | `hgpath/walker.py` suffix walk, inst/leaf roles | zigzag hierarchy specs |
| 7 | S7 | `hgpath/batch.py` prefix cluster + sequential resolve | 200-check read=1 |
| 8 | S8 | `hgpath/handoff.py` → FlatRow + scoped_files | parity `flat_rows_from_resolve` |
| 9 | S9 | `scripts/run_hgpath.py` CLI, `hgpath.report` + milestones | hgrep_demo RUN |
| 10 | S10 | `hgconn/scoped.py` open only tree scoped_files | file count test |
| 11 | S11 | `hgconn/bloom.py` word-boundary RHS/LHS probe | no false neg tests |
| 12 | S12 | `hgconn/walk.py` coarse forward/back on inst chain | zigzag conn subset |
| 13 | S13 | `scripts/run_hgconn.py` CLI, `hgconn.report` | demo checks |
| 14 | S14 | `run_hgpath_hgconn.py` orchestrator (optional) | e2e timing |
| 15 | S15 | zigzag full matrix + bench vs legacy | CI markers |
| 16 | S16 | branch parallel (`--jobs`) only at fork | bench only |

## Log milestones (hgpath)

- `hgpath milestone flat-ready modules=N rtl_files=M`
- `hgpath milestone tree-ready nodes=N`
- `hgpath milestone cluster checks=C unique_prefixes=P`
- `hgpath milestone lpm spec=... hit=... suffix_hops=K`
- `hgpath milestone check-done id=... status=... inst_hops=... leaf=... files=... elapsed_ms=...`

## Log milestones (hgconn)

- `hgconn milestone scoped files=N`
- `hgconn bloom probe file=... lhs=... rhs=... hit=word|assign|miss`
- `hgconn milestone check-done id=... connected=... mode=bloom elapsed_ms=...`

## Report shape (concise)

**hgpath.report:** totals, cache hit%, top shared prefixes, per-check one-line path table.  
**hgconn.report:** totals, connected/fail, bloom vs assign hits, per-check one line.

## DB files (under `.db_{TOP}/`)

- `hgpath_flat.json` — same schema as `grep_hie.json` (module_index, rtl_paths)
- `hgpath_tree.json` — path → nodes[], scoped_files, port_tail, ok
- `hgpath_manifest.json` — schema_version, rtl_paths hash, timestamps