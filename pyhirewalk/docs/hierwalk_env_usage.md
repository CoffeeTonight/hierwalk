# EDA filelist environment variables — how they are actually used

## Mental model (simulator)

```text
shell:
  export PROJ=/proj/chip
  export RTL_ROOT=$PROJ/rtl
  vcs -f $PROJ/filelist.f …

process environ  ──►  every path token in .f is expanded  ──►  then path resolved
```

The **same** environment the `vcs`/`xrun`/`vsim` process sees is what expands
strings inside command files (filelists). There is no separate “filelist macro
language” for paths: it is shell-style **`$NAME` / `${NAME}`** substitution on
path-like tokens.

pyhirewalk run JSON:

```jsonc
"env": { "PROJ": "/proj/chip", "RTL_ROOT": "/proj/chip/rtl" }
```

means: **pretend these were exported** before reading the `.f` (no login shell required).

---

## Where `$VAR` appears in a filelist

| Line form | After expand (example) | Then |
|-----------|------------------------|------|
| `$RTL_ROOT/top.sv` | `/proj/chip/rtl/top.sv` | source file |
| `-f $PROJ/ip/uart/files.f` | `-f /proj/chip/ip/uart/files.f` | open nested list; paths **inside** nested use nested’s base (`-f`) |
| `-F $PROJ/tb/tb.f` | absolute path of nested | nested **content** paths relative to **index_cwd** (`-F`) |
| `+incdir+$RTL_ROOT/include+$VIP/inc` | multi dir after expand | include search |
| `-y $TECH_LIB/stdlib` | library dir | `-y` |
| `-v $TECH_LIB/cells.v` | library file | `-v` |
| `+define+PATH=$PROJ` | rare; value expanded | Verilog define |

**Not** the same as top-level JSON `defines` (those are Verilog `+define+` / `` `define``).

---

## Syntax survey: `${var}` vs `$VAR/` vs `{$VAR}`

| Form | Real usage | Evidence | pyhirewalk |
|------|------------|----------|------------|
| **`$VAR`** | **Most common** in shell + `.f` paths | Shell / `os.path.expandvars`; every EDA flow that inherits process env | **Yes** |
| **`$VAR/...`** | **Same as `$VAR`** + path separator | e.g. `$PROJ/rtl/top.sv`, `+incdir+$ROOT/include` (PULP/questasim scripts use `\$ROOT/...`) | **Yes** (not a separate grammar) |
| **`${VAR}`** | **Common** when name must not glue to next letters, or style preference | POSIX; Verilator `-f` docs: `$VAR`, `$(VAR)`, `${VAR}`; DVT: `$var`, `${var}`, `%var%`; rust `verilog-filelist-parser`: `$()` / `${}` | **Yes** |
| **`$(VAR)`** | **Used in open tooling** (make-ish) | Verilator official `-f` help text; filelist parsers; some IDE filelist support | **Yes** |
| **`{$VAR}`** | **Not standard EDA env syntax** | Braces *outside* `$` are not POSIX env form; often typo of `${VAR}` or Tcl-ish. After naive `$VAR` expand becomes `{/path}` which is usually wrong | **No special form** (if someone wrote `{$PROJ}/a`, only `$PROJ` inside matches → `{/path}/a`) |

### Concrete examples (what you put in `.f`)

```text
# Industry-typical
$PROJ/rtl/top.sv
${PROJ}/rtl/top.sv
$PROJ/ip/uart/files.f          # after expand: /proj/.../files.f  then -f semantics
+incdir+${RTL_ROOT}/include+${VIP}/inc

# Verilator / some parsers also accept
$(PROJ)/rtl/top.sv

# Do NOT rely on this as "env form"
{$PROJ}/rtl/top.sv             # non-standard; becomes {/proj}/rtl/... if $PROJ expands
```

### Shell vs filelist

- **`$VAR/`**: in the shell and in filelists this is simply **variable + `/`**.  
  There is no separate “slash form” to implement.
- **`${var}`**: required for glued suffixes: `${PROJ}_run/filelist.f` → `/path_run/filelist.f`.  
  Bare `$PROJ_run` would look up env name `PROJ_run`.

## Expansion rules we implement

1. **Syntax:** `$NAME` | `${NAME}` | `$(NAME)` with `NAME = [A-Za-z_][A-Za-z0-9_]*`  
2. **Boundaries:** `$PROJ` does **not** expand inside `$PROJECT`  
3. **Order per token:** strip quotes → expand env → if relative, join content base → abspath  
4. **Env map:** `os.environ` + JSON `env` (JSON wins)  
5. **Unset:** leave token literal and **error** (`Unset environment variable ${X}…`)  
6. **`-f` vs `-F`:** env expand first; only *relative path base* differs after that  

---

## hierwalk wiring (for comparison)

```text
apply_config_env_from_document → os.environ
parse_filelist (often without env=)
expand_filelist → replace + os.path.expandvars
```

pyhirewalk does the same **intent**, with safer identifier expansion and explicit
unset diagnostics:

```text
JSON env → os.environ + env map
audit log before filelist
expand_eda_env on every path token / +incdir+ body / -f -F -y -v sources
```

---

## Company checklist

1. List every `$FOO` / `${FOO}` in your top and nested `.f` files.  
2. Put the same names under run JSON `"env"`.  
3. Run `python3 build_db.py --config run.json` and read  
   `config-env: … before filelist parse` plus any `Unset environment variable`.  
4. Keep Verilog macros under `"defines"`, not `"env"`.
