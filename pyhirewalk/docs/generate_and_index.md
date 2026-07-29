# Generate vs essential index (company RTL)

Company designs **require** `generate` (`for` / `if` / `case`, often nested with
`` `ifdef ``). That is first-class for hierarchy and connectivity — not optional.

## Two different jobs

| Job | Needs generate unfold? | How |
|-----|------------------------|-----|
| **Essential DB** — “module *name* is defined in which *file*?” | **No** | `build_db --mode fast` text scan (or slow `pyslang` full parse) |
| **Hierarchy / instance paths** — `top.g_ch[i].u_x` | **Yes** | pyslang **elaborate** under a fixed compile context |
| **hie2hie / relate / COI** | **Yes** | same: elaborated structure + dataflow on a **scope slice** |

`generate` does not invent new *module definitions* in most flows; it invents
**instances** (and sometimes bind choices) of modules that already have source
files. So a fast name→file catalog remains valid and necessary.

What generate *does* invent is the **live instance tree** for one context
(defines + parameters). That cannot be recovered by grepping `module` alone.

## Why not full-chip pyslang at build_db time

Elaborating *all* 10k+ listed RTL every catalog build is what made ~40 min runs.
Generate does **not** force that cost onto the catalog step.

Correct split:

```text
1) build_db (fast)     → SQLite: files + modules(name→file)     [minutes]
2) on query / hie2hie  → resolve needed module files from DB
                         → pyslang compile/elaborate **only that closure**
                         → generate for/if/case folded for *this* context
                         → thin hierarchy + COI graph
```

Scoped elaborate still **fully honors generate** inside the modules that are
actually opened. Blackboxes / stubs stop expansion until the frontier needs them.

## What pyslang must do (later phases — required)

Under one `CompileContext` (filelist + env + defines + top/params):

1. Preprocess `` `ifdef `` so only live arms remain.
2. Elaborate `generate for` → concrete indices (`g_ch[0]`, `g_ch[1]`, …).
3. Elaborate `generate if/else` / `case` → only the taken branch’s instances.
4. Record thin instances: `hier_path`, `type_name`, `origin=generate_*`, `gen_index`.
5. Port binds and dependency edges use **those** instance paths, not unrolled text.

Fast scan limitations (acceptable for step 1 only):

- May list a `module` that sits only under inactive ifdef (name still maps to file).
- Does not emit `top.g[i].u` rows — that is step 2+.

## Policy (fixed)

- **Never** treat generate as “edge case” for COI/hierarchy.
- **Never** require full-design elaborate just to build the module catalog.
- **Always** use context-keyed (defines/params) elaborate when building instance
  trees or connectivity involving generate.
