---
name: path-walk-pp-log
description: >
  Diagnose hierwalk path-walk tier0/tier1 scope bugs from pp-t0 logs before
  performance tuning. Use when user mentions pp-t0, tier0, path-walk slow,
  thru=N/M, text-conn opening all RTL, scoped resolve, or HIERWALK_PP_LOG.
  Also use proactively when path-walk/connect work starts on large SoC designs.
---

# path-walk pp-t0 diagnosis

## Contract (non-negotiable)

Text-conn path-walk opens **only RTL on the walked path / scoped filelist subtree**.
Opening all `_sources` during confident resolve is a **bug**, not a perf knob.

Read `history_fn2keep.md` §「진단 체크리스트」before proposing cache, Rust scanner, or regex changes.

## Step 1 — Get or reproduce pp log

```bash
export HIERWALK_PP_LOG=1
# run connect / path-walk; capture stderr or .hier-walk.log
```

## Step 2 — Count and sample (run yourself)

```bash
grep -c '\[hier-walk pp\] pp-t0' LOG
grep '\[hier-walk pp\] pp-t0' LOG | awk '{print $4}' | sort | uniq -c | sort -rn | head -20
```

Red flags:

- pp-t0 count >> files on the connectivity path (e.g. thousands vs handful)
- filenames from unrelated filelists / `DW_*` / noise stubs
- heartbeat `thru=X/Y` with Y ≈ total RTL in design

## Step 3 — Map signal to code (check in order)

| Symptom | Likely cause | File:function |
|---------|--------------|---------------|
| mass pp-t0 on first miss | queue seeded from all `_sources` | `path_walk_db.py:_tier0_regex_queue_scan`, `_tier0_queue_seed_sources` |
| child resolve scans whole design | scoped fallback to `_sources` | `_scoped_sources_for_rtl`, `_scoped_pool_for_policy` |
| pp-t0 continues across unrelated resolves | stale `_regex_queue` | `_ensure_regex_candidates` must `clear()` queue per resolve |

Env: `HIERWALK_PW_TIER0_GLOBAL=1` re-enables design-wide queue seed (default **off**).

## Step 4 — Verify fix

```bash
pytest tests/test_path_walk_db.py::test_tier0_confident_skips_unrelated_filelist_rtl -q
pytest tests/test_path_walk_db.py tests/test_path_walk_resolve_policy.py -q
```

Re-run connect with `HIERWALK_PP_LOG=1`; confirm pp-t0 only hits path-relevant RTL.
pp-t0 detail should include scope tags (`scoped:confident`, `root:recovery`, etc.).

## Do NOT do first

- SQLite / pickle cache tuning
- Rust hw-scan integration
- re2 vs re benchmarks
- tier1 include / define changes

…until Step 2 shows pp-t0 count is proportional to walked scope.