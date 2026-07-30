"""
Run configuration JSON (company-style compile context).

Mirrors the practical need filled by EDA tool +define / filelist options and by
hierwalk-style input JSON — without importing hierwalk.

Supports JSON and JSONC (// and /* */ comments). Relative paths resolve against
the config file's directory.

``env`` / ``environment`` (like hierwalk): object of shell variables used inside
``.f`` paths (``$PROJ``, ``${RTL_ROOT}/…``). Applied to ``os.environ`` and passed
into filelist expansion.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union


@dataclass(frozen=True)
class ConnCheck:
    """One a[]/b[] hierarchy-group connectivity check (fanout / fanin roles)."""

    id: str
    a: tuple[str, ...]  # typically fanout / source group
    b: tuple[str, ...]  # typically fanin / sink group
    # optional role override; default a=fanout, b=fanin
    a_role: str = "fanout"
    b_role: str = "fanin"
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class RunConfig:
    """Compile + action settings loaded from a single company-style run JSON."""

    filelist: Path
    top: str = ""
    index_cwd: Optional[Path] = None
    defines: Dict[str, str] = field(default_factory=dict)
    # Filelist / path $VAR expansion (also applied to os.environ)
    env: Dict[str, str] = field(default_factory=dict)
    env_applied: tuple[str, ...] = ()
    # parallel workers (build_db scan / future jobs)
    jobs: int = 0
    # essential DB build
    db_path: Optional[Path] = None
    work_dir: Optional[Path] = None
    modules_json: Optional[Path] = None  # modulename→filepath map output/input
    # hier_resolve path list (optional; prefer run_conn_check for groups)
    resolve_paths: tuple[str, ...] = ()
    # connectivity checks (hier_conn)
    conn_checks: tuple[ConnCheck, ...] = ()
    # bookkeeping
    config_path: Optional[Path] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def defines_cli_form(self) -> List[str]:
        out: List[str] = []
        for k, v in sorted(self.defines.items()):
            out.append(k if v == "1" else f"{k}={v}")
        return out

    def filelist_env(self) -> Dict[str, str]:
        """Env map for expand_filelist: process env + config overrides."""
        merged = dict(os.environ)
        merged.update(self.env)
        return merged


def strip_json_comments(text: str) -> str:
    """Remove // line comments and /* */ block comments outside of strings."""
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    str_q = ""
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == str_q:
                in_str = False
            i += 1
            continue
        if ch in "\"'":
            in_str = True
            str_q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def loads_json_document(text: str) -> Any:
    """Parse JSON or JSONC text."""
    cleaned = strip_json_comments(text)
    # trailing commas (common in hand-edited configs)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def read_json_document(path: Union[str, Path]) -> Any:
    p = Path(path).expanduser()
    return loads_json_document(p.read_text(encoding="utf-8-sig"))


def parse_defines(data: Any) -> Dict[str, str]:
    """
    Accept:
      {"FOO": "1", "WIDTH": "8"}
      ["FOO", "WIDTH=8", "BAR=0"]
      "FOO=1"  (single string)
    """
    if data is None:
        return {}
    if isinstance(data, Mapping):
        out: Dict[str, str] = {}
        for k, v in data.items():
            key = str(k).strip()
            if not key:
                continue
            if v is None:
                out[key] = "1"
            elif isinstance(v, bool):
                out[key] = "1" if v else "0"
            else:
                out[key] = str(v)
        return out
    if isinstance(data, str):
        data = [data]
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        out = {}
        for item in data:
            s = str(item).strip()
            if not s:
                continue
            if "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
            else:
                out[s] = "1"
        return out
    raise ValueError("'defines' must be an object, array of MACRO[=VAL], or string")


def parse_env_block(data: Any) -> Dict[str, str]:
    """
    Accept env object (hierwalk-compatible)::

      "env": { "PROJ": "/work/chip", "RTL_ROOT": "/work/chip/rtl" }

    ``null`` values mean unset (pop) when applied; they are omitted from the
    returned map used for substitution.
    """
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValueError("'env' must be an object of NAME → value")
    out: Dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip()
        if not key:
            continue
        if v is None:
            continue
        out[key] = str(v).strip()
    return out


def apply_env(
    env: Mapping[str, str],
    *,
    overwrite: bool = True,
    unset: Optional[Sequence[str]] = None,
) -> List[str]:
    """
    Apply config env to ``os.environ`` (hierwalk default: JSON wins).

    Returns list of keys applied.
    """
    applied: List[str] = []
    for key, val in env.items():
        if not overwrite and key in os.environ:
            continue
        os.environ[key] = val
        applied.append(key)
    for key in unset or ():
        k = str(key).strip()
        if k:
            os.environ.pop(k, None)
            applied.append(k)
    return applied


def apply_env_from_document(
    doc: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> tuple[Dict[str, str], List[str]]:
    """
    Read ``env`` / ``environment`` from document, apply to process, return
    (env_map_for_substitution, applied_keys).

    hierwalk order: apply → audit → filelist parse (expandvars sees os.environ).
    """
    block = _get(doc, "env", "environment", "hierwalk_env", "hier-walk-env")
    if block is None:
        return {}, []
    if not isinstance(block, Mapping):
        raise ValueError("'env' must be an object of environment variable names to values")

    # null → unset
    to_set: Dict[str, str] = {}
    to_unset: List[str] = []
    for k, v in block.items():
        key = str(k).strip()
        if not key:
            continue
        if v is None:
            to_unset.append(key)
        else:
            to_set[key] = str(v).strip()

    applied = apply_env(to_set, overwrite=overwrite, unset=to_unset)
    return to_set, applied


def format_env_audit_lines(
    env: Mapping[str, str],
    *,
    applied: Sequence[str] = (),
    defines: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """
    stderr-friendly audit, mirroring hierwalk's
    \"after JSON env, before filelist parse\".
    """
    lines = [
        "config-env: === run environment (after JSON env, before filelist parse) ===",
    ]
    if not env:
        lines.append("config-env: JSON env block: (none)")
    else:
        declared = [f"{k}={env[k]}" for k in sorted(env)]
        lines.append(
            f"config-env: JSON env block declared ({len(declared)}): "
            + "; ".join(declared)
        )
        if applied:
            lines.append(
                "config-env: JSON env applied to process "
                f"({len(applied)}): {', '.join(sorted(set(applied)))}"
            )
        # Spot-check effective os.environ for declared keys
        for k in sorted(env):
            eff = os.environ.get(k)
            if eff != env[k]:
                lines.append(
                    f"config-env: WARNING {k} effective={eff!r} != config={env[k]!r}"
                )
    if defines:
        parts = [f"{k}={v}" for k, v in sorted(defines.items())]
        lines.append(
            f"config-env: verilog-defines from JSON ({len(parts)}): "
            + "; ".join(parts[:40])
            + (f" … +{len(parts) - 40} more" if len(parts) > 40 else "")
        )
    else:
        lines.append("config-env: verilog-defines from JSON: (none)")
    lines.append(
        "config-env: note: path $VAR in .f uses process environ + config env; "
        "defines are +define+/`define only (not path vars)"
    )
    return lines


def expand_env_string(s: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Expand EDA-style ``$VAR`` / ``${VAR}`` (identifier-safe; see envexpand)."""
    from pyhirewalk.filelist.envexpand import expand_eda_env

    out, _missing = expand_eda_env(s, env, keep_unset=True)
    return out


def _get(doc: Mapping[str, Any], *keys: str) -> Any:
    lower_map = {str(k).lower().replace("-", "_"): v for k, v in doc.items()}
    for key in keys:
        k = key.lower().replace("-", "_")
        if k in lower_map:
            return lower_map[k]
        if key in doc:
            return doc[key]
    return None


def _resolve(
    base: Path,
    raw: Any,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    if raw is None:
        return None
    s = expand_env_string(str(raw).strip(), env)
    if not s:
        return None
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    else:
        p = p.resolve()
    return p


def parse_path_list(data: Any) -> List[str]:
    """Hierarchy path list: array of strings, or newline string."""
    if data is None:
        return []
    if isinstance(data, str):
        return [
            ln.strip()
            for ln in data.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        out: List[str] = []
        for item in data:
            s = str(item).strip()
            if s and not s.startswith("#"):
                out.append(s)
        return out
    raise ValueError("path list must be an array of hierarchy strings")


def parse_conn_checks(data: Any) -> List[ConnCheck]:
    """
    Accept run_conn_check **object with checks array**, or a bare checks list::

      { "checks": [ { "id", "a", "b" }, ... ], "blabla": ... }  // blabla ignored
      [ { "id", "a", "b" }, ... ]

    Does **not** treat sibling keys of ``checks`` (e.g. blabla) as check entries.
    """
    if data is None:
        return []
    checks_raw = data
    if isinstance(data, Mapping):
        if "checks" in data:
            checks_raw = data["checks"]
        elif "check" in data:
            checks_raw = data["check"]
        elif any(k in data for k in ("a", "b", "id")):
            # single check object
            checks_raw = [data]
        else:
            raise ValueError(
                "run_conn_check object must have a 'checks' array; "
                f"found keys {list(data.keys())!r} (siblings are not checks)"
            )

    out: List[ConnCheck] = []
    if isinstance(checks_raw, Mapping):
        # id → {a,b} map form only when user passed checks as object
        for cid, body in checks_raw.items():
            if not isinstance(body, Mapping):
                raise ValueError(f"check '{cid}' must be an object with a/b")
            out.append(_one_conn_check(str(cid), body))
        return out
    if isinstance(checks_raw, Sequence) and not isinstance(checks_raw, (str, bytes)):
        for i, body in enumerate(checks_raw):
            if not isinstance(body, Mapping):
                raise ValueError(f"checks[{i}] must be an object")
            cid = str(body.get("id") or body.get("name") or f"check_{i}")
            out.append(_one_conn_check(cid, body))
        return out
    raise ValueError("run_conn_check.checks must be a JSON array or id→object map")


def _one_conn_check(cid: str, body: Mapping[str, Any]) -> ConnCheck:
    a = parse_path_list(_get(body, "a", "src", "sources", "fanout", "group_a"))
    b = parse_path_list(_get(body, "b", "dst", "sinks", "fanin", "group_b"))
    a_role = str(_get(body, "a_role", "a-role") or "fanout").strip().lower()
    b_role = str(_get(body, "b_role", "b-role") or "fanin").strip().lower()
    # pass through unknown keys for future options
    known = {
        "id",
        "name",
        "a",
        "b",
        "src",
        "dst",
        "sources",
        "sinks",
        "fanout",
        "fanin",
        "group_a",
        "group_b",
        "a_role",
        "a-role",
        "b_role",
        "b-role",
    }
    extra = {str(k): v for k, v in body.items() if str(k) not in known}
    return ConnCheck(
        id=cid,
        a=tuple(a),
        b=tuple(b),
        a_role=a_role,
        b_role=b_role,
        extra=extra,
    )


def load_run_config(
    path: Union[str, Path],
    *,
    apply_process_env: bool = True,
    env_overwrite: bool = True,
) -> RunConfig:
    """
    Load a run config JSON/JSONC file (one document for build_db / resolve / conn).

    Recognized keys (snake or kebab case):
      filelist (required)
      top
      jobs | workers | scan_workers
      cwd | index_cwd | index-cwd
      defines          — `ifdef / +define+  (e.g. NO_CPU: \"1\")
      env | environment — shell vars for .f path expansion
      db | output | db_path
      work_dir | work-dir
      modules_json | modules_map
      build_db: { output, work_dir, mode, scan_workers, modules_json }
      hier_resolve | resolve: { paths: [...] }  or paths: [...]
      run_conn_check | conn_check: { checks: [ {id, a, b}, ... ] }
    """
    cfg_path = Path(path).expanduser().resolve()
    base = cfg_path.parent
    doc = read_json_document(cfg_path)
    if not isinstance(doc, Mapping):
        raise ValueError(f"run config must be a JSON object: {cfg_path}")

    env_map, applied = apply_env_from_document(doc, overwrite=env_overwrite)
    if not apply_process_env:
        # re-read without side effect: already applied — caller should not use this often
        env_map = parse_env_block(
            _get(doc, "env", "environment", "hierwalk_env", "hier-walk-env")
        )
        applied = []

    fl_raw = _get(doc, "filelist")
    if not fl_raw:
        raise ValueError(f"run config missing 'filelist': {cfg_path}")
    filelist = _resolve(base, fl_raw, env=env_map)
    if filelist is None:
        raise ValueError("filelist path empty")

    top = str(_get(doc, "top") or "").strip()
    cwd = _resolve(
        base,
        _get(doc, "cwd", "index_cwd", "index-cwd"),
        env=env_map,
    )
    defines = parse_defines(_get(doc, "defines"))

    jobs_raw = _get(doc, "jobs", "workers", "n_jobs", "n-jobs")
    jobs = int(jobs_raw) if jobs_raw is not None and str(jobs_raw).strip() != "" else 0

    db_path = _resolve(
        base, _get(doc, "db", "output", "db_path", "db-path"), env=env_map
    )
    work_dir = _resolve(base, _get(doc, "work_dir", "work-dir"), env=env_map)
    modules_json = _resolve(
        base,
        _get(doc, "modules_json", "modules-json", "modules_map", "modules-map"),
        env=env_map,
    )

    build_blk = _get(doc, "build_db", "build-db")
    if isinstance(build_blk, Mapping):
        if db_path is None:
            db_path = _resolve(
                base,
                _get(build_blk, "db", "output", "db_path", "path"),
                env=env_map,
            )
        if work_dir is None:
            work_dir = _resolve(
                base, _get(build_blk, "work_dir", "work-dir"), env=env_map
            )
        if modules_json is None:
            modules_json = _resolve(
                base,
                _get(
                    build_blk,
                    "modules_json",
                    "modules-json",
                    "modules_map",
                    "map",
                ),
                env=env_map,
            )
        sw = _get(build_blk, "scan_workers", "scan-workers", "jobs", "workers")
        if sw is not None and jobs <= 0:
            jobs = int(sw)

    # hier_resolve paths (flat list; groups live under run_conn_check)
    resolve_paths: List[str] = []
    resolve_blk = _get(doc, "hier_resolve", "hier-resolve", "resolve")
    if isinstance(resolve_blk, Mapping):
        resolve_paths = parse_path_list(
            _get(resolve_blk, "paths", "path", "list", "hierarchies")
        )
    elif resolve_blk is not None:
        resolve_paths = parse_path_list(resolve_blk)
    if not resolve_paths:
        resolve_paths = parse_path_list(_get(doc, "paths", "hierarchies"))

    conn_blk = _get(
        doc,
        "run_conn_check",
        "run-conn-check",
        "conn_check",
        "conn-check",
        "hier_conn",
        "hier-conn",
    )
    conn_checks = parse_conn_checks(conn_blk) if conn_blk is not None else []
    # also allow top-level "checks"
    if not conn_checks:
        top_checks = _get(doc, "checks")
        if top_checks is not None:
            conn_checks = parse_conn_checks(top_checks)

    return RunConfig(
        filelist=filelist,
        top=top,
        index_cwd=cwd,
        defines=defines,
        env=dict(env_map),
        env_applied=tuple(applied),
        jobs=jobs,
        db_path=db_path,
        work_dir=work_dir,
        modules_json=modules_json,
        resolve_paths=tuple(resolve_paths),
        conn_checks=tuple(conn_checks),
        config_path=cfg_path,
        raw=dict(doc),
    )


def _normalize_hierarchy_string(raw: Any) -> str:
    """JSON string → hierarchy path; strip accidental surrounding quotes."""
    if not isinstance(raw, str):
        raise ValueError(
            f"hierarchy path must be a JSON string, got {type(raw).__name__}: {raw!r}"
        )
    s = raw.strip()
    # double-encoded / copy-paste: "\"top.x\"" or "'top.x'"
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if not s or s.startswith("#"):
        return ""
    # reject JSON-looking garbage (keys/braces scraped as text)
    if any(ch in s for ch in "{}\"'"):
        raise ValueError(
            f"invalid hierarchy path (looks like JSON noise, not a hier name): {raw!r}"
        )
    return s


def hierarchy_paths_from_checks(checks: Sequence[ConnCheck]) -> List[str]:
    """Flatten ConnCheck a∪b (dedup, order preserved)."""
    out: List[str] = []
    seen: set[str] = set()
    for ch in checks:
        for p in list(ch.a) + list(ch.b):
            s = str(p).strip()
            if not s or s.startswith("#") or s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def hierarchy_paths_from_config(
    cfg: RunConfig,
    *,
    include_resolve_paths: bool = False,
) -> List[str]:
    """Default: only conn_checks a∪b."""
    out = hierarchy_paths_from_checks(cfg.conn_checks)
    if not include_resolve_paths:
        return out
    seen = set(out)
    for p in cfg.resolve_paths:
        s = str(p).strip()
        if s and not s.startswith("#") and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def extract_hierarchies_from_run_conn_checks(doc: Mapping[str, Any]) -> List[str]:
    """
    **Strict** hierarchy extraction for hier_resolve.

    Reads **only** (after real JSON parse, never text-scrape)::

        doc["run_conn_check"]["checks"]  // must be a JSON array
          [i]["a"]  // must be array of strings
          [i]["b"]  // must be array of strings

    Ignored on purpose:
      - run_conn_check.blabla / any sibling of "checks"
      - nested run_conn_check.nested.checks
      - top-level "checks", "paths", "hier_resolve"
      - id, a_role, comment, meta, ... inside a check object
    """
    # exact keys only (no treating whole object as id→check map)
    conn = None
    for key in (
        "run_conn_check",
        "run-conn-check",
        "conn_check",
        "conn-check",
    ):
        if key in doc:
            conn = doc[key]
            break
    if conn is None:
        return []
    if not isinstance(conn, Mapping):
        raise ValueError("'run_conn_check' must be a JSON object")

    if "checks" not in conn:
        raise ValueError(
            "'run_conn_check' must contain a 'checks' array "
            "(other keys like blabla/description are ignored and must not replace checks)"
        )
    checks = conn["checks"]
    if not isinstance(checks, list):
        raise ValueError("'run_conn_check.checks' must be a JSON array [...]")

    out: List[str] = []
    seen: set[str] = set()
    for i, item in enumerate(checks):
        if not isinstance(item, Mapping):
            raise ValueError(f"run_conn_check.checks[{i}] must be a JSON object")
        for ab in ("a", "b"):
            if ab not in item:
                continue
            arr = item[ab]
            if not isinstance(arr, list):
                raise ValueError(
                    f"run_conn_check.checks[{i}].{ab} must be a JSON array of strings"
                )
            for j, elem in enumerate(arr):
                try:
                    s = _normalize_hierarchy_string(elem)
                except ValueError as e:
                    raise ValueError(
                        f"run_conn_check.checks[{i}].{ab}[{j}]: {e}"
                    ) from e
                if not s or s in seen:
                    continue
                seen.add(s)
                out.append(s)
    return out


def load_hier_resolve_inputs(
    path: Union[str, Path],
) -> tuple[List[str], Dict[str, str], Optional[Path]]:
    """
    hier_resolve --config loader (JSON parse, not text scrape).

    Uses:
      - run_conn_check.checks[].a / .b   → hierarchies (not path env)
      - defines                         → `ifdef
      - env | environment               → expand $VAR in modules_json / map paths
      - modules_json | build_db.modules_json → optional map

    Does not walk run_conn_check siblings (blabla, …) as checks.
    Does not use env to invent hierarchy strings.
    """
    cfg_path = Path(path).expanduser().resolve()
    base = cfg_path.parent
    doc = read_json_document(cfg_path)
    if not isinstance(doc, Mapping):
        raise ValueError(f"run config must be a JSON object: {cfg_path}")

    paths = extract_hierarchies_from_run_conn_checks(doc)
    defines = parse_defines(doc.get("defines"))

    env_map = parse_env_block(
        doc.get("env") if "env" in doc else doc.get("environment")
    )
    # also allow hierwalk aliases if present as keys
    if not env_map:
        for k in ("hierwalk_env", "hier-walk-env"):
            if k in doc:
                env_map = parse_env_block(doc.get(k))
                break

    modules_json = _resolve(
        base,
        doc.get("modules_json")
        or doc.get("modules-json")
        or doc.get("modules_map")
        or doc.get("modules-map"),
        env=env_map or None,
    )
    build_blk = doc.get("build_db") or doc.get("build-db")
    if modules_json is None and isinstance(build_blk, Mapping):
        modules_json = _resolve(
            base,
            build_blk.get("modules_json")
            or build_blk.get("modules-json")
            or build_blk.get("modules_map")
            or build_blk.get("map"),
            env=env_map or None,
        )
    if modules_json is None:
        db = None
        if isinstance(build_blk, Mapping):
            db = _resolve(
                base,
                build_blk.get("db")
                or build_blk.get("output")
                or build_blk.get("db_path")
                or build_blk.get("path"),
                env=env_map or None,
            )
        if db is None:
            db = _resolve(
                base,
                doc.get("db") or doc.get("output") or doc.get("db_path"),
                env=env_map or None,
            )
        if db is not None:
            cand = db.with_suffix(".modules.json")
            if cand.is_file():
                modules_json = cand

    return paths, defines, modules_json


def merge_run_config(
    cfg: RunConfig,
    *,
    filelist: Optional[Union[str, Path]] = None,
    top: Optional[str] = None,
    index_cwd: Optional[Union[str, Path]] = None,
    defines: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    db_path: Optional[Union[str, Path]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    cli_defines_override: bool = False,
) -> RunConfig:
    """
    Overlay CLI values on a loaded config.

    - Path/top/db: non-empty CLI wins.
    - defines / env: by default **merge** (CLI overrides same keys).
    """
    merged_env = {**cfg.env, **dict(env or {})}
    if env:
        apply_env(env, overwrite=True)

    fl = Path(filelist).resolve() if filelist else cfg.filelist
    tp = top if top is not None and str(top).strip() != "" else cfg.top
    cwd = Path(index_cwd).resolve() if index_cwd else cfg.index_cwd
    db = Path(db_path).resolve() if db_path else cfg.db_path
    wd = Path(work_dir).resolve() if work_dir else cfg.work_dir

    if defines is None:
        defs = dict(cfg.defines)
    elif cli_defines_override:
        defs = dict(defines)
    else:
        defs = {**cfg.defines, **dict(defines)}

    return replace(
        cfg,
        filelist=fl,
        top=tp,
        index_cwd=cwd,
        defines=defs,
        env=merged_env,
        db_path=db,
        work_dir=wd,
    )
