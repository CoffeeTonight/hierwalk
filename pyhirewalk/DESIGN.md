# pyhirewalk — Design

> Standalone project. No dependency on `hierwalk`.  
> Goal: **group-to-group RTL connectivity / COI** on large, preprocessor- and generate-heavy designs, with a **lazy, cache-friendly knowledge graph**.

---

## 1. Product primary use case

Most common query:

> Given two **bundles** (groups) of signals/ports/slices (or hierarchy boundaries expanded to ports),  
> report **which endpoints in Gₛ connect to which in Gₜ**, and **how** (combo / through FF / cut / unknown).

| Sugar API | Expands to |
|-----------|------------|
| `hie2hie(A, B)` | `relate(boundary(A), boundary(B))` |
| `path(a, b)` | `relate({a}, {b})` + optional path explain |
| `relate(Gₛ, Gₜ)` | **core** |

Default engine: **zigzag** (multi-source meet-in-the-middle), not single-direction COI only.

---

## 2. Hard realities of industrial RTL

The tool must treat these as **first-class**, not edge cases.

### 2.1 Preprocessor

- `` `define`` / `` `include`` / `` `ifdef`` / `` `ifndef`` / `` `elsif`` / `` `endif``
- Command-line and filelist `+define+` change **which modules, ports, and instances exist**
- Same path string under different defines = **different design** → always keyed by `context_id`

### 2.2 Filelist

- Nested `-f` / `-F` (VCS semantics), `+incdir+`, `+define+`, `-y`/`-v`
- 10k+ listed files; **effective + queried** subset is much smaller
- Provenance: which `.f` listed which source

### 2.3 Generate and structural choice

Generate is not “unroll later decoration”; it **is** hierarchy and connectivity.

| Pattern | Example intent | Tool requirement |
|---------|----------------|------------------|
| `for` generate | `for (genvar i=0; i<N; i++)` bank of instances | Elaborate **or** symbolic range; instance names `g[i].u` |
| `if` / `else` generate | ASIC vs FPGA block, width-dependent structure | Only **active** branch under param/define context |
| Nested `for` + `if` | `for` over channels, inner `if (i!=0)` pipeline stage | Per-index structural variant |
| `case` generate | tech/feature mux of implementations | One arm live per context |
| Arrayed instances | `u_reg[0:3]` or generate-equivalent | Thin array descriptor; lazy element expand |
| Conditional instance (ifdef) | whole IP present only if `` `ifdef HAS_X`` | Missing ≠ error if inactive; no phantom connect |
| Conditional ports | port list differs by ifdef | Boundary bundle must use **effective** ports |
| Multiple candidate modules | same instance slot, different module via generate/ifdef | Bind **one** `type_id` per context after elab |
| Blackbox / encrypted / missing | no body | Stub boundary; zigzag stops with `stub_cut` |
| Interfaces / modports | bundle-like ports | Expand to interface signals when needed |
| XMR / hierarchical ref | cross-scope force/read | Explicit edge kind `xmr`; may pull remote scopes |

**Rule:** Analysis always runs on an **elaborated structural snapshot** for a fixed `CompileContext` (params + defines).  
We never answer connectivity from raw un-preprocessed text alone.

### 2.4 Buses and multi-dimensional ports

- Packed / unpacked 2D–3D arrays are common
- Queries often name **different slices of the same base signal**
- **Do not** materialize one graph node per bit or per array element by default

---

## 3. Layered architecture

```text
┌─────────────────────────────────────────────────────────┐
│  Query: relate / hie2hie / path                         │
│  Engine: ZigzagEngine (default)                         │
├─────────────────────────────────────────────────────────┤
│  L5  Query cache: GroupRelation, cones, path detail     │
│  L4  Slice intern (canonical SliceDesc → slice_id)      │
│  L3  Word-level dependency KG (lazy along frontier)     │
│  L2  Thin hierarchy (instance → type_id, param bind)    │
│  L1  Module/file index (definitions, provenance)        │
│  L0  CompileContext (filelist expand + defines + cwd)   │
└─────────────────────────────────────────────────────────┘
         ▲ pyslang: preprocess, parse, elaborate (scoped)
```

| Layer | Responsibility | Not responsible for |
|-------|----------------|---------------------|
| L0 | What to compile | Connectivity |
| L1 | Where is `module foo` | Instance tree |
| L2 | Instance paths, type bind, generate result names | Bit-level nets |
| L3 | `net → net` data dependence (combo/seq) | Full-chip always-on graph |
| L4 | Slice identity | Hierarchy |
| L5 | User-facing relation results | Parsing RTL |

---

## 3.1 Borrowed features: study usage first

When lifting an idea from hierwalk, **trace the real call path** before coding.
Example: JSON ``env`` is not “a dict passed into expand only”:

1. apply to ``os.environ`` (whole run context)
2. audit log *before* filelist parse
3. filelist expand uses ``expandvars`` (process env) for ``$PROJ`` paths
4. **separately**, top-level ``defines`` → Verilog ``+define+`` / `` `define``

Details: `docs/hierwalk_env_usage.md`.

## 3.2 Generate is mandatory (not a fast-scan excuse)

Company RTL **depends on** `generate for/if/case` (often with ifdef). Hierarchy
paths and COI are only meaningful after **context-scoped elaboration**.

| Phase | Generate | Mechanism |
|-------|----------|-----------|
| `build_db` name→file | not required | `mode=fast` scan (default); avoids 40 min full parse |
| Instance tree / hie2hie / relate | **required** | pyslang elaborate on **slice/closure** from DB, not whole 13k every time |

See `docs/generate_and_index.md`.

---

## 4. Compile context (L0)

Every artifact is partitioned by:

```text
context_id = hash(
  top_filelist, index_cwd,
  listed sources, incdirs,
  defines (+ CLI extra),
  top module name(s),
  library dirs/files, slang options,
  schema_version
)
```

**Implementation:** `pyhirewalk.context.CompileContext` + `filelist.expand` (`-f`/`-F`).

pyslang input: **flattened** absolute filelist (no nested `-f`/`-F`).

---

## 5. Structural model (L1–L2) under generate / ifdef

### 5.1 Definitions vs instances

```text
ModuleType (shared)
  type_id
  module_name, def_file, def_line
  port_decls[]     # effective ports under this type/param variant
  param_template
  structural_fingerprint  # optional: hash of child instance template after elab of this type

Instance (thin)
  inst_id, hier_path, inst_name
  parent_id
  type_id
  param_overrides   # only what affects structure/widths
  origin            # plain | generate_for | generate_if | generate_case | ifdef | array
  gen_index_path    # e.g. [i=3, j=0] for nested for
  gen_block_name    # named generate block if any
```

**Same RTL module** instantiated 5000× → **one** `ModuleType`, many thin `Instance` rows.

### 5.2 Generate representation

Do **not** store unrolled AST. Store **elaborated outcomes**:

| Source construct | Stored as |
|------------------|-----------|
| `for (i=0; i<N; i++) begin : bank ... end` | N instances (or compact `ArrayInst` if isomorphic) with `gen_index_path=[i]` |
| `if (P) begin : g_on ... end else ...` | Only the live branch’s children |
| Nested for+if | Instances that exist after condition fold under context |
| Inactive ifdef region | **Absent** (no rows) |

**Compact array form** (optional optimization when isomorphic):

```text
ArrayInstGroup:
  base_path, type_id, index_ranges[], conn_template_id
  # expand element e only when frontier touches base[e] or query names it
```

### 5.3 Multiple structural choices (same “logical slot”)

Example patterns:

```systemverilog
`ifdef USE_V2
  foo_v2 u_foo (...);
`else
  foo_v1 u_foo (...);
`endif

// or
if (USE_PIPE)
  pipe_stage u0 (...);
else
  assign y = x;  // no instance
```

Under a fixed context:

- Exactly **one** binding is visible (or none)
- `hier_path` like `top.u_foo` maps to **one** `type_id`
- Cache/DB must **not** merge v1/v2 types across contexts

Cross-context comparison is a **future** feature; default is single-context truth.

### 5.4 Parameter-dependent structure

Params that change generate bounds or if-conditions fold into:

```text
type_key = (module_name, def_loc, params_affecting_structure)
```

Width-only params may keep the same `type_id` with different port width metadata.

---

## 6. Endpoints, bundles, slices (L4 + query model)

```text
SliceDesc   = canonical packed/unpacked selects  (intern → slice_id)
SignalRef   = (scope_inst_id, signal_or_port_name)  # or net_id after bind
Endpoint    = (SignalRef, slice_id)   # FULL slice = whole signal
Bundle      = sorted unique endpoints + origin expression (user string / hier expand)
```

**Multi-dim ports:** shape metadata once on port/signal; queries attach `SliceDesc`.  
Identical `[7:0]` across the design → **one** `slice_id`.

Bundle key:

```text
bundle_key = sha(sorted(endpoint_keys))
```

---

## 7. Dependency knowledge graph (L3)

Directed multigraph on **word-level nets** (default):

```text
Node: net_id | port_boundary | ff_q | stub
Edge: src → dst  (src influences dst)
  kind: combo | seq | port | xmr | opaque
  loc: file:line (optional)
  # slice overlays only when refined:
  #   src_slice_id, dst_slice_id
```

| Edge kind | Meaning |
|-----------|---------|
| `combo` | combinational dependence |
| `seq` | through FF / sequential element (D→Q class) |
| `port` | hierarchy cross via port connection |
| `xmr` | hierarchical reference |
| `opaque` | blackbox in→out assumption (policy) |

**FF policy modes:**

- `combo`: do not traverse `seq`
- `through_ff`: traverse `seq`, count hops
- `both`: report best/labels separately

Graph is built **lazily along zigzag frontier**, not for all 13k files up front.

---

## 8. Zigzag engine (default relate algorithm)

Real connectivity **ping-pongs** across sibling arms, bridges, and hubs.  
Unidirectional fanout from Gₛ alone misses many industrial paths.

### 8.1 Algorithm sketch

```text
relate(G_s, G_t, mode, budget):
  S = normalize(G_s);  T = normalize(G_t)
  frontier_S, frontier_T = S, T
  labels_S[n] = bitset of sources; labels_T similarly for sinks
  pairs = {}

  while budget.ok and not coverage_done:
    if wave is S_forward:
      expand fanout(frontier_S) by one wave (respect mode)
      open modules only when frontier demands (lazy)
      meet = new_nodes ∩ (seen_T ∪ frontier_T)
    else:
      expand fanin(frontier_T) by one wave
      meet = new_nodes ∩ (seen_S ∪ frontier_S)

    for m in meet:
      pairs ∪= labels_S[m] × labels_T[m]   # join at witness

    zigzag: flip direction
      (or adaptive: expand the side with smaller frontier / fewer file opens)

  return GroupRelation(pairs, unconnected, meets, stubs, stats)
```

### 8.2 Modes

| Mode | Behavior |
|------|----------|
| `zigzag` | **default** — alternate S fwd / T bwd |
| `forward` | S fanout only; test membership in T |
| `backward` | T fanin only |
| `waypoint_zigzag` | force intermediate bundle W: relate(S,W) then (W,T) |
| `bounded_*` | max_waves, max_files, max_ff_hops, max_nodes |

### 8.3 Why not “LCA spine files only”

LCA path is a **hint** for priority open, not a closed world.  
Lateral generate bridges and ifdef’d mux paths often leave the ancestor spine.  
**Frontier-driven open** is authoritative.

### 8.4 Path explain (optional)

Full paths are **not** stored for every pair.  
On demand: from witness `m`, reconstruct S-side and T-side chains.

---

## 9. GroupRelation result

```text
GroupRelation:
  rel_key, context_id
  bundle_s_key, bundle_t_key
  mode, budget_tag
  pairs: [
    { src_ep, dst_ep, kind, min_ff_hops, meet_points[], confidence }
  ]
  unconnected_src[], unconnected_dst[]
  intermediate_ffs[]          # optional union
  stubs[]                     # stops
  stats: files_opened, nodes_visited, waves, cache_hits
```

**Default report:** edge list (pairs).  
Matrix only when |S|×|T| is small or user asks.  
Path dump only with `--show-path` / API flag.

Confidence:

| Value | Meaning |
|-------|---------|
| `exact` | edges fully resolved in open RTL |
| `overapprox` | word-level may over-connect vs bit intent |
| `stub_cut` | blackbox/missing stopped expansion |
| `budget_cut` | stopped by limits; may be incomplete |

---

## 10. Cache policy

### 10.1 Principles

1. **Share immutable structure** (types, shapes, slice_ids).  
2. **Thin instances** — no per-bit hierarchy nodes.  
3. **Word-first graph** — slice refine on hit pairs only.  
4. **Cone caches serve many group queries.**  
5. **LRU on query results**; keep L1/L2 longer.  
6. **All keys include `context_id` + `schema_version`.**

### 10.2 Cache table

| ID | Contents | Key | Invalidation |
|----|----------|-----|--------------|
| C0 | flat slang `.f` | context ingredients | filelist/defines mtime |
| C1 | L1 module↔file DB | `context_id` | def file / header hash |
| C2 | L2 thin hier | `context_id` + top + param env | structural RTL / defines |
| C3 | L3 edge chunks | `context_id` + module/type or slice region | module body change |
| C4 | slice intern | canonical SliceDesc | context wipe |
| C5a | multi-source **fwd cone** | `(context, bundle_key, mode)` | C3 |
| C5b | multi-source **bwd cone** | symmetric | C3 |
| C5c | **GroupRelation** | `(context, b_s, b_t, mode, budget_tag)` | C5a/b |
| C5d | path detail | pair + mode | LRU aggressive |

**Partial reuse:** if cone(S_big) cached and S_small ⊂ S_big → project labels.  
**Zigzag benefit:** fixed S, varying T → reuse C5a; only recompute bwd side.

### 10.3 Dedup rules (hierarchy × arrays)

| Anti-pattern | Policy |
|--------------|--------|
| Clone full port list per instance | type-level `port_decls` only |
| Node per `data[i][j][k]` | shape + SliceDesc intern |
| Edge per slice string spelling | canonical slice_id on overlay |
| Full-chip bit matrix | forbidden |
| Cache without defines | forbidden |

### 10.4 Budgets (required guards)

```text
max_files_opened
max_nodes_visited
max_waves
max_ff_hops
max_pairs_reported
max_slice_refines
```

Exceed → `budget_cut` + best-effort pairs so far (never silent full-design scan).

---

## 11. Design patterns catalog (must handle)

Checklist for engine + tests (toy RTL fixtures, not hierwalk suite import):

### Preprocessor / choice

- [ ] Ifdef module body / instance / port  
- [ ] Nested ifdef  
- [ ] Define from filelist vs header vs CLI merge order  
- [ ] Include path order  

### Generate

- [ ] Simple `for` instance bank  
- [ ] `for` + inner `if (i==0)` special case  
- [ ] Nested `for` (2D array of instances)  
- [ ] `if/else` generate swapping two implementations  
- [ ] `case` generate  
- [ ] Named generate blocks in hier paths  
- [ ] genvar-dependent port connections / bit selects  

### Hierarchy / bind

- [ ] Deep vs shallow sibling arms + bridge (zigzag topology)  
- [ ] Arrayed instances  
- [ ] Blackbox middle  
- [ ] Interface / modport boundary  
- [ ] Parameterized width between bundles  

### Data path

- [ ] Combo-only path  
- [ ] Multi-FF path  
- [ ] Handshake loops (cycle + seen set)  
- [ ] Y-fork / Y-merge  
- [ ] Bus slice A[7:0] ↔ B[15:8] partial  
- [ ] Concat / multi-driver (policy: overapprox or explicit)  

### Query shapes

- [ ] Two large port groups (matrix/edges)  
- [ ] Instance boundary ↔ instance boundary  
- [ ] Waypoint hub  
- [ ] Unconnected members reported  

---

## 12. pyslang role

| Task | Owner |
|------|--------|
| Preprocess, parse, type, elaborate | pyslang |
| Definition locations → L1 | pyslang + index builder |
| Instance tree after generate/ifdef fold → L2 | pyslang elab (scoped when possible) |
| Fine dataflow / netlist edges → L3 | pyslang analysis and/or future netlist helper; **not** regex-as-truth |
| Filelist `-f`/`-F` | **our** expander (pyslang lacks full `-F` semantics) |
| Zigzag / bundles / cache | **our** engine |

Scoped elaborate: prefer compiling **closure of frontier modules + stubs** for children not yet needed; expand stubs when zigzag reaches them.

---

## 13. Work directory layout

```text
work/<context_id>/
  meta.json
  flat.slang.f
  index.sqlite      # L1 (+ optional L2)
  graph/            # L3 chunks optional
  cache/
    cones/
    relations/
```

---

## 14. Implementation phases

| Phase | Deliverable |
|-------|-------------|
| **0** | Filelist expand + `CompileContext` + `context_id` ✅ |
| **1** | L1 SQLite: files + symbols from pyslang; cache C0/C1 |
| **2** | L2 thin hierarchy for a top; generate/ifdef effective instances |
| **3** | Endpoint/Bundle/SliceDesc; boundary expand |
| **4** | L3 lazy word edges (minimal viable) |
| **5** | Zigzag `relate`; GroupRelation edges report |
| **6** | Cone + relation caches; budgets; slice refine on hits |
| **7** | Waypoints, opaque blackbox policy, richer reports |

---

## 15. Non-goals (explicit)

- Rebuilding hierwalk path-walk / text-regex connectivity as source of truth  
- Full-chip always-resident bit-level graph  
- Cross-context “merge all ifdef worlds” into one graph  
- Silent success when budget or stubs truncated the search  

---

## 16. One-sentence architecture

> **Under a fixed compile context, build a thin elaborated hierarchy and a lazy word-level dependency graph; answer bundle-to-bundle questions with a budgeted zigzag meet-in-the-middle engine; share types/shapes/slices aggressively; never expand generate/ifdef alternatives that are not live in that context.**
