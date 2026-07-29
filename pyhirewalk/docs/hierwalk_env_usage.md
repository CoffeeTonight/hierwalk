# How hierwalk used `env` (what we should have read first)

This note captures **actual call order** in `~/Desktop/hierwalk`, so pyhirewalk does not
half-port a feature without knowing its role.

## Two different jobs for one JSON key

In hierwalk run JSON, `"env": { ... }` is **not** only for RTL `+define+`.

| Kind | Examples | Consumer |
|------|----------|----------|
| **A. Path / EDA tree vars** | `PROJ`, `RTL_ROOT`, `DESIGN` | filelist lines: `$PROJ/…`, `${RTL_ROOT}/ip.f` |
| **B. Tool behavior knobs** | `HIERWALK_LAZY`, `HIERWALK_PW_DB_BUILD`, `HIERWALK_JOBS`, `HCH_INDEX_CWD` | read later from **`os.environ`** by path-walk, cache, jobs, … |

`defines` is a **separate** top-level key → Verilog `` `define`` / `+define+` macros.  
Do not mix path vars into `defines`.

## Real call sequence (hierwalk)

```text
1. Load run JSON
2. apply_config_env_from_document(doc)   # → writes os.environ (JSON wins by default)
3. emit config-env audit on stderr
     "after JSON env, before filelist parse"
4. parse_filelist(filelist, index_cwd=…)
     # often WITHOUT an explicit env= dict argument
5. expand_filelist:
     custom replace on env dict (often empty)
     then os.path.expandvars(s)          # ← path $VAR comes from process environ
6. Later code reads HIERWALK_* from os.environ for tool policy
```

Evidence:

- `run_request.apply_config_env_from_document` only mutates `os.environ`.
- `cli_execute.parse_filelist(...)` does **not** pass `env=…`.
- `hch_compat.filelist_preprocess.expand_env` ends with `os.path.expandvars`.
- `config_env_audit` logs JSON env + behavioral `HIERWALK_*` **before** filelist parse.

So the **design intent** is:

> Config env is process-level context for the whole run, not a private bag only for one function.

## Why company filelists break without this

Typical `.f`:

```text
-f $PROJ/ip/uart/filelist.f
+incdir+${RTL_ROOT}/include
```

If JSON never applied `PROJ` / `RTL_ROOT` to the process (or expand path),  
expand reports missing nested filelists / sources.

## What pyhirewalk must do (aligned)

1. Load JSON → parse `env` / `environment`.
2. **Apply to `os.environ` first** (same as hierwalk; JSON overwrites shell by default).
3. **Log audit** (declared + applied) before filelist expand.
4. Expand filelist with `os.environ` + overrides (`$VAR` / `${VAR}`).
5. Keep `defines` only for Verilog macros / `+define+`.
6. Resolve config path fields (`filelist`, `cwd`, `db`) with the same env.

Behavior knobs named `HIERWALK_*` are **not** reimplemented unless we add that feature;
unknown keys still go to `os.environ` so paths work and future tools can read them.
