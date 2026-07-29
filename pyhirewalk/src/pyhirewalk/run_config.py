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
class RunConfig:
    """Compile + action settings loaded from a run JSON."""

    filelist: Path
    top: str = ""
    index_cwd: Optional[Path] = None
    defines: Dict[str, str] = field(default_factory=dict)
    # Filelist / path $VAR expansion (also applied to os.environ)
    env: Dict[str, str] = field(default_factory=dict)
    env_applied: tuple[str, ...] = ()
    # essential DB build
    db_path: Optional[Path] = None
    work_dir: Optional[Path] = None
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


def load_run_config(
    path: Union[str, Path],
    *,
    apply_process_env: bool = True,
    env_overwrite: bool = True,
) -> RunConfig:
    """
    Load a run config JSON/JSONC file.

    Recognized keys (snake or kebab case):
      filelist (required)
      top
      cwd | index_cwd | index-cwd
      defines
      env | environment   — shell vars for .f path expansion
      db | output | db_path   (essential sqlite path)
      work_dir | work-dir
      build_db: { output, work_dir }  optional nested block
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

    db_path = _resolve(
        base, _get(doc, "db", "output", "db_path", "db-path"), env=env_map
    )
    work_dir = _resolve(base, _get(doc, "work_dir", "work-dir"), env=env_map)

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

    return RunConfig(
        filelist=filelist,
        top=top,
        index_cwd=cwd,
        defines=defines,
        env=dict(env_map),
        env_applied=tuple(applied),
        db_path=db_path,
        work_dir=work_dir,
        config_path=cfg_path,
        raw=dict(doc),
    )


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
