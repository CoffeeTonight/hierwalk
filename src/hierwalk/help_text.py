"""Detailed CLI and JSON configuration help text."""

from __future__ import annotations

HELP_DESCRIPTION = """\
hier-walk — regex-based Verilog instance scan and structural connectivity.

Modes (pick one; default is hierarchy dump):
  hierarchy          Flat instance list TSV (default)
  find-top           Top-module candidates
  search             Instance / path search
  check-connect      Single endpoint-pair connectivity
  check-connect-batch  Many pairs; JSON or text pairs file
  fanin-cone / fanout-cone  COI cone debug (FF/port/blackbox boundaries)
  inst-trace             Driver/sinker trace from one instance path (JSON)

Pass RUN.json as the first argument to supply all options from one file. CLI flags override JSON.
See --help-config, --help-connect, --help-cone, and --help-inst-trace."""

HELP_EPILOG = """\
examples:
  hier-walk design.f --top SOC_TOP -o instances.tsv
  hier-walk design.f --find-top -o tops.tsv
  hier-walk design.f --top top --search "u_ecc*,idx" -o hits.tsv
  hier-walk design.f --top top --check-connect top.clk top.u0.clk
  hier-walk design.f --top top --check-connect-batch checks.json -o conn.tsv
  hier-walk run.json -o out.tsv
  hier-walk run.json --no-cache --define DEBUG=1

JSON help:
  hier-walk --help-config     full run JSON field reference
  hier-walk --help-connect    connectivity batch JSON only
  hier-walk --help-cone       fanin/fanout cone mode
  hier-walk --help-stress     random RTL connectivity stress / pytest
  hier-walk --example         write search_example.json (all search features)

environment:
  HIERWALK_LAZY              default on: minimal index (includes only), scoped connect
                              elab, lazy filelist; macro/ifdef/body on connect/elab
                              (HIERWALK_LAZY=0 for eager/full index)
  HIERWALK_LAZY_IFDEF        when lazy: run ifdef during index (default off)
  HIERWALK_CACHE_DIR         override per-top work dir (.db_{TOP}); default is local .db_{TOP}
  HIERWALK_IGNORE_PATH       default --ignore-path patterns (comma-separated)
  HIERWALK_IGNORE_MODULE     default --ignore-module names (comma-separated)
  HIERWALK_IGNORE_FILELIST   default --ignore-filelist patterns (comma-separated)
  HIERWALK_INCLUDE_WARM      opt-in include warm before parallel preprocess
  HIERWALK_NO_INCLUDE_WARM   skip include discovery/warm entirely
  HIERWALK_INCLUDE_WARM_MAX  max includes to warm (default 200; 0 = no limit)
  HIERWALK_PW_DB_BUILD       off | after_verify (full tier-1 DB after conn/cone/trace)
  HIERWALK_PW_DB_PREFETCH    legacy alias: 1 => after_verify
  HIERWALK_PW_DB_PREFETCH_WAIT  wait for post-verify DB build (default 1; 0=detach)
  HIERWALK_PW_DB_PREFETCH_MAX   cap post-verify DB files per run (0 = no limit)
  HIERWALK_PW_TRACE_VERBOSE  show tier0/tier1 pw-db search steps on stderr (1=on)
  HIERWALK_PW_HEARTBEAT      periodic pw-db / connect-coi progress (1=30s)
  HIERWALK_CONNECT_JOBS      path-walk text/logical COI worker count (0=auto)
  HIERWALK_PP_LOG            preprocessing tags on stderr: 0=off 1=brief (default) 2=all
  HIERWALK_PP_LOG_SLOW_MS    min ms for pp-closure at brief level (default 1000)
  HIERWALK_LOG_SLOW_FILES    log per-file preprocess/scan timing (1=10s, or seconds)
  HIERWALK_LOW_MEMORY_AUTO   auto fused index above N sources (default 1500; 0=off)
  HCH_INDEX_CWD               default --index-cwd for -F filelists"""

CONFIG_HELP = """\
hier-walk run JSON (positional RUN.json)
==================================

All CLI options can be expressed in one JSON object. Relative paths are resolved
against the directory containing the JSON file.

Required
--------
  filelist (string)
      Top Verilog filelist (.f). Same as the positional argument.
      ``$VAR`` / ``${VAR}`` expand from the process environment (setenv/export).
      RTL paths inside the ``.f`` use the same expansion.

Mode (optional; inferred when omitted)
--------------------------------------
  mode (string)
      hierarchy | find-top | search | check-connect | check-connect-batch
      | cone | path-walk
  find-top (bool)             Same as --find-top
  all-tops (bool)             Same as --all-tops

Elaboration / filelist
----------------------
  top (string)                Top module; auto-pick when exactly one candidate
  defines (object | array)    Extra +define macros.
                              Object: {"USE_PCIE": "1", "DEBUG": "1"}
                              Array:  ["USE_PCIE=1", "DEBUG"]
  index-cwd (string)          EDA cwd for -F nested filelists (--index-cwd);
                              also ``$VAR`` / ``HCH_INDEX_CWD`` env
  max-depth (int)             Max instance elaboration depth (--max-depth)

Output / logging
----------------
  output (string)             TSV path; "-" for stdout (default: "-")
  quiet (bool)                Suppress stderr progress (--quiet)
  log-file (string)           Append run report to this path (--log-file)
  no-log-file (bool)          Disable default run log (--no-log-file)

Search mode
-----------
  search (string | object)    Instance/path/hierarchy search (see below)
  search-path (string)        Legacy hierarchy path glob (merged into object form)
  search-subtree (bool)       Include instances under matched hierarchies
  search-module (bool)        Also match module type names
  search-case-insensitive (bool)
                              Case-insensitive globs (default: false)

  Structured search object (``search`` as object)::
    instance (string|array)       Match inst_leaf (and module if search-module)
    path (string|array)           Full-path segment globs (see syntax below)
    hierarchy_path (string|array)   Fixed-depth hierarchy + optional port verify
    case_insensitive (bool)         Per-query case policy (default: false)
    search_module (bool)          Also match module type names
    search_subtree (bool)         Include instances under matched hierarchies

  Path pattern syntax (``search.path``, dotted ``--search``)::
    ``.``       Next hierarchy segment — fixed depth, aligned from root (top first).
    ``..``      One or more intermediate segments; the only token that spans hops.
    ``*`` ``?`` Globs within a single node name; ``*`` never crosses ``.``.
    Regex       Per-segment when ``+ ( ) { } | ^ $ \\`` appear, or ``re:`` prefix
                (compiled once per pattern; e.g. ``er_[0-9]+[xyz]``).
    No dots     Any one path segment may match (e.g. ``*niu*``).

    ``a.b.*c``                  Exactly three segments from root.
    ``top.u_spine.*``           Exactly three segments; third is any child name.
    ``top.E*..*log.*cpu*``      ``E*``, then 1+ hops, then ``*log*``, then ``*cpu*``
                                on the next segment (adjacent to ``*log*``).
    ``top.E*..*log..*cpu*``     Same, but at least one segment between ``*log*`` and
                                ``*cpu*``.

    Patterns do not skip segments via ``*`` alone; use ``..`` for variable depth.
    ``--search-path`` / ``hierarchy_path``: same ``.`` / ``..`` / ``*`` rules, plus
    optional trailing port segment (e.g. ``top.u_*.clk``).

  Legacy flat ``search`` string: comma-separated patterns; dotted patterns
  route to path matching, plain patterns to instance matching.

Connectivity — single
---------------------
  check-connect (array | object)
      Two endpoints: ["top.a", "top.b"] or {"a": "...", "b": "..."}
  connect-trace (bool)        TSV hops + terminal path report (--connect-trace)
  connect-log (bool)          Alias for connect-trace (JSON/scripts)
  include-ff (bool)           Traverse always_ff D->Q edges (--include-ff)
  ff-barrier (bool)           Inverse of include-ff (include_ff = !ff_barrier)
  strict-generate (bool)      Strict generate folding for connectivity
  over-approximate-if (bool|null)
                              if/generate over-approximation policy

Cone mode (COI debug)
---------------------
  fanin-cone (string)         Endpoint for fanin cone (--fanin-cone)
  fanout-cone (string)        Endpoint for fanout cone (--fanout-cone)
  cone-graph (string)         Optional Graphviz DOT path (--cone-graph)
  over-approximate-if (bool|null)
                              Same policy as connectivity (default: true)

  Boundaries (stop + collect):
    fanout: always_ff D (ff-sink), module outputs (port-out), blackboxes
    fanin:  always_ff Q (ff-driver), module inputs (port-in), blackboxes
    ignore-hierarchy: ``ignore_hierarchy`` pattern matched (see below)
    trace-depth: ``trace_max_depth`` exceeded (top = depth 0)

  ignore_hierarchy (string | array)
    Hierarchy instance globs for trace stop (cone + inst-trace).
    ``top.a.b.c.*`` — blackbox strict descendants of ``c`` (not ``c`` itself).
    ``top.a.b.c`` — ``c`` and all below.  Plain integers in the array set depth
    (same as ``trace_max_depth``; minimum wins).
  trace_max_depth (int)
    Stop tracing below this hierarchy depth (top = 0). Origin endpoint exempt.

  Terminal report lists flip-flops, ports, and blackboxes; TSV lists boundaries.
  Uses a separate index path — does not slow default --check-connect.

Example (fanout from a net)
---------------------------
{
  "filelist": "design.f",
  "mode": "cone",
  "top": "top",
  "fanout-cone": "top.u_mid.din",
  "output": "cone.tsv"
}

Inst-trace mode (instance drivers / sinkers)
--------------------------------------------
  mode: inst-trace | inst_trace
  inst_trace (object|string)  Required. Instance hierarchy path only.

  inst_trace fields:
    instance (string)         Hierarchy path (e.g. top.u_blk)
    direction (string)        driver | in | sinker | out | both  (default: both)
    path_kind (string)        ff | comb  (default: ff; aliases: ff_comb, ff/comb)
    top, defines              Optional overrides
    ignore_hierarchy          Trace stop globs (same rules as cone mode)
    trace_max_depth           Max hierarchy depth from top (origin exempt)

  driver/in  — fanin from input (and inout) ports; collect port-in, ff-driver, …
  sinker/out — fanout from output (and inout) ports; collect port-out, ff-sink, …
  path_kind ff   — traverse always_ff D<->Q interior
  path_kind comb — stop at ff-driver / ff-sink boundaries

Example
-------
{
  "filelist": "design.f",
  "top": "SOC_TOP",
  "mode": "inst-trace",
  "inst_trace": {
    "instance": "SOC_TOP.u_ecc",
    "direction": "both",
    "path_kind": "ff"
  },
  "output": "inst_trace.tsv"
}

Path-walk connect (on-demand index)
-----------------------------------
  mode: path-walk | path_walk
      Skips full filelist index. Per-check hierarchy walk (jobs for trie branches),
      then text/logical COI with shared mod_cache. connect_jobs (JSON or
      HIERWALK_CONNECT_JOBS) parallelizes COI; pipeline overlaps hierarchy of
      check N+1 with COI of check N when connect_jobs > 1.

  Requires connect / check-connect-batch / check-connect plus top.

  Stress corpus (4 sets × 10-deep zigzag × 10-bit array):
    python -m hierwalk.path_walk_stress_gen --out-dir DIR
    hier-walk DIR/pw_stress.run.json -o connect.tsv
    pytest tests/test_path_walk_stress.py -q

  Example (array bit × driver matrix):
  {
    "filelist": "design.f",
    "mode": "path-walk",
    "top": "SOC_TOP",
    "connect": {
      "checks": [
        {"id": "b0", "a": "SOC_TOP.u_blk.arr[0]", "b": "SOC_TOP.drv_a"},
        {"id": "b1", "a": "SOC_TOP.u_blk.arr[1]", "b": "SOC_TOP.drv_b"}
      ]
    },
    "output": "connect.tsv"
  }

Flat run suite (one JSON, sibling blocks, sequential run)
-------------------------------------------------------
  Top-level siblings (same level). Each block has enable: 1|0 to run or skip.
  filelist, top, defines stay at the root; full-index options live under
  run_on_full_index.

  run_on_full_index          Full filelist index + elaboration settings
    enable (0|1)             Run hierarchy/search/find-top step when 1
    mode                     hierarchy (default) | search | find-top
    ignore-path              RTL path globs (moved here from top level)
    ignore-path-file         External ignore lists
    ignore-module            Module names to mark ignorePath
    ignore-filelist          Listing .f names/paths to skip
    jobs, no-cache, cache-dir, refresh-cache, low-memory, max-depth, …
    output                   TSV for hierarchy/search/find-top step

  run_conn_check             P2P path connectivity (checks inside)
    enable (0|1)
    mode                     full-index | path-walk (default: path-walk)
    checks                   Endpoint pairs (required)
    output

  run_io_trace               Instance driver/sinker trace
    enable (0|1)
    mode                     full-index | path-walk (default: path-walk)
    instance, direction, path_kind (ff|comb)
    ignore_hierarchy, trace_max_depth, output

  run_cone_trace             Fanin/fanout COI cone
    enable (0|1)
    mode                     full-index | path-walk (default: path-walk)
    fanin_cone / fanout_cone (pick one), cone-graph
    ignore_hierarchy, trace_max_depth, output

  Verification block ``mode`` is only the index strategy (full-index vs path-walk).
  The step kind is the block name (run_conn_check, run_io_trace, run_cone_trace).
  Legacy values (check-connect-batch, inst-trace, fanout-cone, …) map to full-index.

  run_on_full_index.enable (0|1) is independent from verification block enable flags.
    1 — run the hierarchy/search/find-top step; settings merge into verification steps
    0 — block fully inactive: no step, no settings merge, no jobs/no_cache from block
    (omitted) — defaults to 0 when run_conn_check / run_io_trace / run_cone_trace exist,
                else 1. Per-step enable on verification blocks does NOT disable this block.
  When enable is 0, verification blocks cannot use full-index strategy either (forced
  path-walk even if mode says full-index or a legacy alias like hierarchy).
  Put per-step ignores/jobs/cache on each verification block when full-index is off.
  Legacy key run_on_full_db is still accepted.

Example (flat)
--------------
{
  "filelist": "design.f",
  "top": "top",
  "defines": {"USE_X": "1"},
  "run_on_full_index": {
    "enable": 0,
    "mode": "hierarchy",
    "ignore_path": ["pcielinktop"],
    "ignore_module": ["bb_mod"],
    "jobs": 4,
    "no_cache": true,
    "output": "instances.tsv"
  },
  "run_conn_check": {
    "enable": 1,
    "mode": "path-walk",
    "checks": [{"id": "a", "a": "top.a", "b": "top.z"}],
    "output": "conn.tsv"
  },
  "run_io_trace": {
    "enable": 1,
    "mode": "full-index",
    "instance": "top.u_mid",
    "direction": "driver",
    "path_kind": "ff",
    "output": "trace.tsv"
  },
  "run_cone_trace": {
    "enable": 0,
    "mode": "full-index",
    "fanout_cone": "top.u_mid.din",
    "output": "cone.tsv"
  }
}

Legacy tests[] array is still accepted (enable supported per entry).

Bundled examples (run from examples/stress_seed42):
  flat_run_example.json    (JSONC: // comments; one runnable step + commented templates)
  path_walk_example.json   (path-walk jobs + HIERWALK_PW_DB_* env; EN+KO table in header)
  stress_42_d8.suite.json  (all steps enabled)
  search_example.json      (all search features; or hier-walk --example)

  cd examples/stress_seed42
  hier-walk flat_run_example.json
  hier-walk path_walk_example.json
  hier-walk search_example.json

Connectivity — batch (single-test / legacy)
-------------------------------------------
  connect (object)            Inline checks + connect options (preferred in run JSON)
  check-connect-batch (string | object)
      Path to pairs/checks file, OR inline same shape as connect

  connect / check-connect-batch object fields:
      checks (array)          Required for connect/path-walk; omit for inst-trace/cone/search
      pairs, connections      Aliases for checks
      All run JSON fields     Same surface as RUN.json (filelist, mode, top, output,
                              inst_trace, fanin_cone, search, ignore-*, cache, jobs, …)
      include-ff, connect-trace, trace, strict-generate, over-approximate-if

  Batch output TSV columns:
      check_id, endpoint_a, endpoint_b, connected, mode, note, errors, hops

  Missing hierarchy/port: fails before COI search; errors column lists evidence
  (nearest path, elab roots, child instances, declared ports, etc.).

Ignore rules
------------
  ignore-path (string | array)       RTL path globs (--ignore-path)
  ignore-path-file (string | array)  External ignore lists (--ignore-path-file)
  ignore-module (string | array)     Module names (--ignore-module)
  ignore-filelist (string | array)   Listing .f names/paths (--ignore-filelist)

Environment (optional)
----------------------
  env (object)                Process env for hier-walk tuning (see HELP epilog).
                              Keys like HIERWALK_PP_LOG, HIERWALK_LOG_SLOW_FILES.
                              Aliases: environment, hier-walk-env.
                              JSON env wins over shell export for the same key.

Cache / parallelism
-------------------
  jobs (int)                  Parallel index workers; 0=auto CPU count
  j (int)                     Alias for jobs
  job (int)                   Alias for jobs (typo-tolerant)
  low-memory (bool)           Fused per-file build (less RAM, slower cold index)
  cache-dir (string)          Disk cache directory
  no-cache (bool)             Disable index/elab cache
  refresh-cache (bool)        Force index rebuild

CLI override rule
-----------------
When both RUN.json and CLI flags are present, explicit CLI flags win over JSON.

Example (hierarchy)
-------------------
{
  "filelist": "design.f",
  "top": "SOC_TOP",
  "output": "instances.tsv",
  "defines": {"USE_PCIE": "1"},
  "jobs": 4
}

Example (search)
----------------
{
  "filelist": "design.f",
  "mode": "search",
  "top": "hc_verify_top",
  "search": "idx,ecc",
  "search-module": true,
  "output": "hits.tsv"
}

Example (structured search)
---------------------------
{
  "filelist": "design.f",
  "mode": "search",
  "top": "chip_top",
  "search": {
    "instance": ["u_*cpu*", "*gpu*"],
    "path": ["*.*cpu*", "chip_top.*.u_*"],
    "hierarchy_path": ["chip_top.u_*.*cpu*"],
    "case_insensitive": true,
    "search_module": true
  },
  "output": "hits.tsv"
}

Example (connectivity batch, inline)
------------------------------------
{
  "filelist": "filelist.f",
  "mode": "check-connect-batch",
  "top": "stress_top",
  "no-cache": true,
  "defines": {"STRESS_USE_IN": "1"},
  "include-ff": true,
  "output": "connect.tsv",
  "connect": {
    "checks": [
      {"id": "clk", "a": "top.clk", "b": "top.u0.clk"},
      {"id": "bad", "a": "top.u_missing.clk", "b": "top.clk"}
    ]
  }
}

Bundled example:
  examples/stress_seed42/stress_42_d8.run.json
"""

CONNECT_HELP = """\
hier-walk connectivity batch JSON
=================================

Used with --check-connect-batch FILE, or inline as "connect" in run JSON.

Minimal (pairs only)
--------------------
[
  ["top.clk", "top.u0.clk"],
  ["top.rst_n", "top.u1.clk"]
]

Object with checks
------------------
{
  "top": "stress_top",
  "defines": {"STRESS_USE_IN": "1"},
  "include-ff": true,
  "connect-trace": false,
  "strict-generate": false,
  "checks": [
    {"id": "port_port", "a": "top.probe_in", "b": "top.u_spine.probe_out"},
    {"id": "missing", "a": "top.u_nope.x", "b": "top.clk"}
  ]
}

Check item aliases
------------------
  Endpoints: a/b, from/to, src/dst, endpoint_a/endpoint_b
  Id:        id or name (optional; appears in check_id column)

Options
-------
  top                 Elaboration top when not set on CLI / run JSON
  jobs (int)          Parallel index workers (same as run JSON; 0=auto)
  j / job / workers   Aliases for jobs
  ignore-path         RTL folder patterns; matched on resolved absolute paths
                      (filelist sources and every `include` target)
  ignore-filelist     Listing .f patterns; RTL listed by a matching filelist is
                      skipped (immediate listing + provenance chain)
  no-cache            Disable index/elab disk cache
  refresh-cache       Force index rebuild
  defines             Merged into compile defines (also used at index build
                      when loaded via RUN.json)
  include-ff (bool)   Allow paths through always_ff (default: comb-only)
  ff-barrier (bool)   Shorthand for include_ff = !ff_barrier
  connect-trace       TSV hops + readable path report on terminal (alias: trace)
  connect-log         Same as connect-trace (alias for JSON)
  strict-generate     Strict generate-region folding
  over-approximate-if bool or null

Text-conn vs logical-conn (path-walk / suite)
--------------------------------------------
  Text-conn asks whether signal *names* appear together in RTL connection
  structure (assign RHS, port maps, hierarchy). It is a coarse bloom filter:
  no constant-fold (``assign z = a * 0`` still links ``a``), no parametric
  dim resolution, and flip-flops are *not* barriers (Q/D may be traversed).
  Multi-dimensional buses may bloom at base/slice granularity.

  Logical-conn asks whether a value can actually propagate: bit-precise COI,
  constant/tie-off masks, and FF barriers (unless include-ff).

  In ``connect_phase: both``, TSV columns ``connected_text`` and
  ``connected`` (logical) are independent; logical runs only for text passes
  when gating is enabled.

Path evidence kinds (hops / connect-log)
----------------------------------------
  intra-module    assign/alias/ff within one module
  child-down      parent net -> child instance port
  child-hier      hierarchical reference into child
  parent-up       child port -> parent via instance port map
  parent-hier-ref child port -> parent via hier ref in parent
  inst-blackbox   parent net through instance when child is not hierarchy-walked
                  (output ports must be structurally driven from the input port
                  in the child module body; empty 1-in/1-out stubs passthrough)
  net-alias       coarse net_rep bloom within one module [text-bloom]

  Trace hop suffixes: [text-bloom], [bit-precise], [structural] — see connect TSV
  header comments when using --connect-trace.

Text pairs file (non-JSON)
--------------------------
  One pair per line; tab or whitespace separated; # comments allowed:
    top.clk\\ttop.u0.clk
    top.rst_n top.u1.clk

Output
------
  TSV with header:
    check_id  endpoint_a  endpoint_b  connected  mode  note  errors  hops

  Expanded checks (``[]``, ``{}``, ``loop``) emit one row per sub-check with
  resolved bit/index endpoints (e.g. ``bus[0]  top.a0  top.bus_b[0]``), not
  only the aggregate parent row. ``--connect-trace`` uses the same flattening.

  ``{…}`` concat (not ``[…]``) enforces Verilog MSB-first bit order; literals
  are padding only. ``[…]`` uses index zip (``bit_align``); literals in ``[…]``
  are rejected — use ``{…}`` for exact ordered bit mapping.

Waypoint fanout trace (``map.kind: waypoint-fanout``)
-----------------------------------------------------
  ``a`` = fanout origin list (port/net/inst; inst expands to all ports).
  ``b`` = peer group (port/net/inst prefix). Full fanout to every terminator;
  off-path destinations are reported (scope/net/rtl_line), not omitted.
  Optional ``map.path_kind``: ``comb`` (default) or ``ff``, or an array
  such as ``["ff", "comb"]`` to run both passes in one check (each kind runs a
  full cone BFS; large designs should prefer a single ``path_kind`` when possible).
  Optional ``map.direction``:
    ``fanout`` (default) — fanout from ``a`` only, ``b`` marks peer hits.
    ``both`` — dual fanout: ``a`` fanout vs ``b``, then ``b`` fanout vs ``a``
    (doubles BFS work; with multiple ``path_kind`` values, cost multiplies —
    e.g. ``direction: both`` + ``path_kind: ["ff", "comb"]`` runs up to four passes).
  ``connected`` is true when any terminator has ``waypoint_qualified=Y``
  (direct peer hit or downstream of a peer prefix), not strict bidirectional
  symmetry. For ``direction: both``, one side reaching a qualified peer is enough.
  TSV (``direction: both``): ``side``, ``peer_hit``, ``peer_matched``, …

  Example::

    {"id": "wp", "a": ["top.drv"], "b": ["top.u_blk", "top.u_mem"],
     "map": {"kind": "waypoint-fanout", "direction": "both",
             "path_kind": ["ff", "comb"]}}

Error policy
------------
  Unknown hierarchy or port: check fails immediately (connected=false) with
  errors describing why (path stops at X, elab roots, child instances, etc.).

Bundled example:
  examples/stress_seed42/stress_42_d8.connect.json
"""

CONE_HELP = """\
hier-walk fanin / fanout cone mode
==================================

Standalone COI (cone of influence) traversal for debug: list all flip-flops,
ports, and blackboxes reached from an endpoint. Does not change --check-connect
performance (separate module index with FF endpoint scan).

CLI
---
  hier-walk design.f --top top --fanout-cone top.u_mid.din -o cone.tsv
  hier-walk design.f --top top --fanin-cone top.u_mid.qout -o cone.tsv
  hier-walk design.f --top top --fanout-cone top.sig --cone-graph cone.dot

Run JSON (positional)
-------------------
{
  "filelist": "design.f",
  "top": "top",
  "mode": "cone",
  "fanout-cone": "top.u_mid.din",
  "output": "cone.tsv",
  "cone-graph": "cone.dot"
}

Use fanin-cone OR fanout-cone (not both). Endpoint syntax matches connectivity:
hierarchy path with optional .port (e.g. top.clk, top.u_child.din).

Boundaries
----------
  ff-sink     always_ff D input (fanout stops here)
  ff-driver   always_ff Q output (fanin stops here)
  port-out    module output port (fanout)
  port-in     module input port (fanin)
  blackbox    opaque / no-body instance

Output
------
  TSV: boundary rows (kind, scope, net, module, detail) + # comment stats
  Terminal: grouped report (stderr when -o -, else stdout) — same pattern as
            --connect-trace path reports.

See also: hier-walk --help-config (cone fields in run JSON)
"""

STRESS_HELP = """\
hier-walk random connectivity stress tests
==========================================

Random deep-hierarchy RTL is generated and checked for port-port, port-inst,
and cross-hierarchy connectivity. Use this to benchmark or regression-test
the connectivity engine.

Generate RTL + JSON artifacts (one seed)
----------------------------------------
  python -m hierwalk.stress_gen --seed 42 --standard --out-dir DIR

  Writes: RTL, filelist.f, *.connect.json, *.run.json
  Profiles:
    --standard     linear depth~10 branch~5 single-file (faster)
    (default)      zigzag extreme depth~20 branch~8 multi-file

Run hier-walk on generated artifacts
------------------------------------
  hier-walk DIR/stress_42_d8.run.json -o connect.tsv
  # or
  hier-walk DIR/filelist.f --check-connect-batch DIR/stress_42_d8.connect.json

Random benchmark (N trials, prints timing table)
------------------------------------------------
  python -m hierwalk.stress_gen --trials 10
  python -m hierwalk.stress_gen --trials 10 --standard
  python -m hierwalk.stress_gen --seed 99 --depth 20 --branch-factor 8

Single-trial report (no --out-dir)
----------------------------------
  python -m hierwalk.stress_gen --seed 42 --standard

pytest (CI / regression)
------------------------
  pytest tests/test_stress_connectivity.py -q
  pytest -m stress -q              # marked slow batch trials (see pyproject.toml)
  pytest tests/test_connectivity.py -q

Bundled fixed-seed example:
  examples/stress_seed42/
"""


def print_config_help() -> None:
    print(CONFIG_HELP)


def print_connect_help() -> None:
    print(CONNECT_HELP)


def print_cone_help() -> None:
    print(CONE_HELP)


INST_TRACE_HELP = """\
hier-walk inst-trace mode
=======================

Trace drivers (fanin) and/or sinkers (fanout) from every port on one instance.
No per-port endpoint list required — supply the instance path only.

Run JSON (positional)
-------------------
{
  "filelist": "design.f",
  "top": "top",
  "mode": "inst-trace",
  "inst_trace": {
    "instance": "top.u_mid",
    "direction": "both",
    "path_kind": "ff"
  },
  "output": "trace.tsv"
}

direction
---------
  driver, in, driver-in   Fanin from input/inout ports (what drives the instance)
  sinker, out, sinker-out Fanout from output/inout ports (what the instance drives)
  both                    Both (default)

path_kind (ff/comb)
-------------------
  ff    Traverse always_ff D<->Q (sequential paths; default)
  comb  Combinational cone only; stop at ff-driver / ff-sink

Output TSV columns:
  origin_port, trace_direction, boundary_kind, scope, net, module, detail
"""


def print_inst_trace_help() -> None:
    print(INST_TRACE_HELP)


def print_stress_help() -> None:
    print(STRESS_HELP)