"""Full hier-walk run configuration from JSON."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hierwalk.connect.shared.request import (
    ConnectivityRequest,
    load_connect_request,
    parse_connect_request_json,
    try_parse_connect_request_json,
)
from hierwalk.inst_trace import InstTraceRequest, parse_inst_trace_json
from hierwalk.trace_stop import parse_trace_stop_policy
from hierwalk.search_spec import (
    SearchSpec,
    document_has_search,
    effective_search_spec,
    resolve_search_spec,
)


def strip_jsonc_line_comments(text: str) -> str:
    """Remove ``//`` line comments outside JSON strings (JSONC-style configs)."""
    out: List[str] = []
    for line in text.splitlines():
        cut = len(line)
        in_string = False
        escape = False
        for i, ch in enumerate(line):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if (
                not in_string
                and ch == "/"
                and i + 1 < len(line)
                and line[i + 1] == "/"
            ):
                cut = i
                break
        out.append(line[:cut].rstrip())
    return "\n".join(out)


def loads_json_document(text: str, *, audit: Optional[List[str]] = None) -> Any:
    """
    Parse JSON/JSONC. When ``audit`` is a list, append duplicate-key warnings
    (stdlib ``json`` keeps the **last** value for repeated keys).
    """
    stripped = strip_jsonc_line_comments(text)

    def _object_pairs_hook(pairs: Sequence[Tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen and audit is not None:
                audit.append(
                    f"duplicate JSON key {key!r} in same object — "
                    f"parser keeps the last value only"
                )
            seen.add(key)
            out[key] = value
        return out

    return json.loads(stripped, object_pairs_hook=_object_pairs_hook)


def read_json_document(path: Union[str, Path]) -> Any:
    return loads_json_document(Path(path).read_text(encoding="utf-8-sig"))


RUN_ON_FULL_INDEX = "run_on_full_index"
RUN_ON_FULL_DB_LEGACY = "run_on_full_db"
RUN_CONN_CHECK = "run_conn_check"
RUN_IO_TRACE = "run_io_trace"
RUN_CONE_TRACE = "run_cone_trace"
RUN_TEST_SUITE_KINDS: Tuple[str, ...] = (
    RUN_ON_FULL_INDEX,
    RUN_CONN_CHECK,
    RUN_IO_TRACE,
    RUN_CONE_TRACE,
)


def _full_index_block_key(data: Mapping[str, Any]) -> Optional[str]:
    if _mapping_get_ci(data, RUN_ON_FULL_INDEX) is not None:
        return RUN_ON_FULL_INDEX
    if _mapping_get_ci(data, RUN_ON_FULL_DB_LEGACY) is not None:
        return RUN_ON_FULL_DB_LEGACY
    return None


def is_run_test_suite_document(data: Mapping[str, Any]) -> bool:
    """True when JSON uses flat suite blocks or a legacy ``tests`` array."""
    if "tests" in data:
        return True
    if _full_index_block_key(data) is not None:
        return True
    return any(_mapping_get_ci(data, k) is not None for k in RUN_TEST_SUITE_KINDS[1:])


_BLOCK_ENABLE_KEYS = ("enable", "enabled")


def block_enable_raw(spec: Mapping[str, Any]) -> Any:
    """Read ``enable`` or ``enabled`` from a suite step block."""
    for key in _BLOCK_ENABLE_KEYS:
        hit = _mapping_get_ci(spec, key)
        if hit is not None:
            return hit
    return None


def block_enabled(spec: Mapping[str, Any], *, default: bool = True) -> bool:
    """True when a suite step block is enabled (``enable`` / ``enabled``)."""
    return parse_enable(block_enable_raw(spec), default=default)


def parse_enable(raw: Any, *, default: bool = True) -> bool:
    """Parse ``enable`` as 1/0 (also true/false, yes/no)."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) != 0
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in ("0", "false", "no", "off"):
            return False
        if key in ("1", "true", "yes", "on"):
            return True
        raise ValueError(f"enable must be 0 or 1, got {raw!r}")
    raise ValueError(f"enable must be 0 or 1, got {raw!r}")


def strip_run_suite_blocks(data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop per-step suite blocks so only shared run fields remain."""
    shared_data = dict(data)
    shared_data.pop("tests", None)
    for key in RUN_TEST_SUITE_KINDS:
        shared_data.pop(key, None)
    shared_data.pop(RUN_ON_FULL_DB_LEGACY, None)
    for key in (
        "mode",
        "connect",
        "check_connect",
        "check_connect_batch",
        "inst_trace",
        "inst-trace",
        "fanin_cone",
        "fanin-cone",
        "fanout_cone",
        "fanout-cone",
        "ignore_path",
        "ignore-path",
        "ignore_path_file",
        "ignore-path-file",
        "ignore_module",
        "ignore-module",
        "ignore_filelist",
        "ignore-filelist",
        "jobs",
        "j",
        "job",
        "workers",
        "low_memory",
        "cache_dir",
        "no_cache",
        "refresh_cache",
        "max_depth",
        "index_cwd",
        "index-cwd",
    ):
        shared_data.pop(key, None)
    return shared_data


@dataclass(frozen=True)
class RunConfig:
    """All options needed to run hier-walk (CLI-equivalent)."""

    filelist: str
    top: Optional[str] = None
    find_top: bool = False
    all_tops: bool = False
    output: str = "-"
    index_cwd: Optional[str] = None
    defines: Tuple[Tuple[str, str], ...] = ()
    max_depth: Optional[int] = None
    search: Optional[str] = None
    search_subtree: bool = True
    search_path: Optional[str] = None
    search_module: bool = False
    search_case_insensitive: bool = False
    search_spec: Optional[SearchSpec] = None
    check_connect: Optional[Tuple[str, str]] = None
    check_connect_batch: Optional[str] = None
    check_hgrep: Optional[str] = None
    connect_inline: Optional[Any] = None
    connect_trace: bool = False
    connect_log: bool = False
    include_ff: bool = False
    fanin_cone: Optional[str] = None
    fanout_cone: Optional[str] = None
    cone_graph: Optional[str] = None
    inst_trace: Optional[InstTraceRequest] = None
    strict_generate: bool = False
    over_approximate_if: Optional[bool] = None
    ignore_path: Tuple[str, ...] = ()
    ignore_path_file: Tuple[str, ...] = ()
    ignore_module: Tuple[str, ...] = ()
    ignore_filelist: Tuple[str, ...] = ()
    ignore_hierarchy: Tuple[str, ...] = ()
    trace_max_depth: Optional[int] = None
    jobs: int = 0
    connect_jobs: int = 0
    low_memory: bool = False
    cache_dir: Optional[str] = None
    no_cache: bool = False
    refresh_cache: bool = False
    quiet: bool = False
    log_file: Optional[str] = None
    no_log_file: bool = False
    mode: Optional[str] = None
    index_strategy: str = ""
    flat_suite_step: bool = False
    full_index_step: bool = False
    direct_filelist_cli: bool = False
    verification_step_kind: str = ""
    verification_step_name: str = ""
    verification_phase: str = "both"
    run_config_source: Optional[str] = None

    @property
    def defines_map(self) -> Dict[str, str]:
        return dict(self.defines)

    @property
    def define_list(self) -> List[str]:
        return [f"{k}={v}" if v != "1" else k for k, v in self.defines]


def parse_connect_phase_value(raw: Any) -> str:
    """Parse connect/verification phase (text, logical, both, or hgrep-only gate)."""
    phase = str(raw or "both").strip().lower()
    if phase in ("text", "logical", "both", "hgrep"):
        return phase
    raise ValueError(
        f"connect_phase must be text, logical, both, or hgrep (got {raw!r})"
    )


def _resolve_path(base: Path, value: Optional[str]) -> Optional[str]:
    if value is None or value == "-":
        return value
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str((base / p).resolve())


def _parse_defines(data: Any) -> Dict[str, str]:
    if data is None:
        return {}
    if isinstance(data, Mapping):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out: Dict[str, str] = {}
        for item in data:
            raw = str(item).strip()
            if not raw:
                continue
            if "=" in raw:
                k, v = raw.split("=", 1)
                out[k.strip()] = v.strip()
            else:
                out[raw] = "1"
        return out
    raise ValueError("'defines' must be an object or array of MACRO[=VAL]")


def _parse_jobs(data: Any) -> int:
    if data is None:
        return 0
    if isinstance(data, bool):
        raise ValueError("'jobs' must be an integer")
    if isinstance(data, (int, float)):
        return int(data)
    if isinstance(data, str):
        raw = data.strip().lower()
        if not raw or raw == "auto":
            return 0
        return int(raw)
    raise ValueError("'jobs' must be an integer")


_JOBS_KEY_ALIASES = ("jobs", "j", "job", "workers", "parallel")


def _mapping_get_ci(data: Mapping[str, Any], key: str) -> Any:
    if key in data:
        return data[key]
    key_lower = key.lower()
    for raw_key, value in data.items():
        if isinstance(raw_key, str) and raw_key.lower() == key_lower:
            return value
    return None


_CONFIG_ENV_KEYS = (
    "env",
    "environment",
    "hier-walk-env",
    "hierwalk_env",
)


def _config_env_block(data: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for key in _CONFIG_ENV_KEYS:
        hit = _mapping_get_ci(data, key)
        if hit is not None:
            if not isinstance(hit, Mapping):
                raise ValueError(
                    f"'{key}' must be an object of environment variable names to values"
                )
            return hit
    return None


def apply_config_env_from_document(
    data: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> List[str]:
    """
    Apply ``env`` / ``environment`` / ``hier-walk-env`` from a run or batch JSON.

    By default JSON wins over existing shell ``export`` (``overwrite=True``).
    Pass ``overwrite=False`` to leave variables already set in the shell unchanged.
    """
    block = _config_env_block(data)
    if block is None:
        return []
    applied: List[str] = []
    for raw_key, raw_val in block.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if not overwrite and key in os.environ:
            continue
        if raw_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(raw_val).strip()
        applied.append(key)
    return applied


def _jobs_from_mapping(
    data: Mapping[str, Any],
) -> tuple[int, Optional[str]]:
    """Return (jobs, source_key) when a jobs-like field is present."""
    for key in _JOBS_KEY_ALIASES:
        hit = _mapping_get_ci(data, key)
        if hit is not None:
            return _parse_jobs(hit), key
    return 0, None


def _jobs_from_document(data: Mapping[str, Any]) -> tuple[int, Optional[str]]:
    jobs, src = _jobs_from_mapping(data)
    if src is not None:
        return jobs, src
    full_index_key = _full_index_block_key(data)
    if full_index_key is not None:
        sub = _mapping_get_ci(data, full_index_key)
        if isinstance(sub, Mapping):
            from hierwalk.enable_diagnostics import resolve_block_enabled

            enabled, _ = resolve_block_enabled(
                sub,
                default=True,
                document=data,
                block_key=full_index_key,
            )
            if enabled:
                nested_jobs, nested_src = _jobs_from_mapping(sub)
                if nested_src is not None:
                    return nested_jobs, f"{full_index_key}.{nested_src}"
    for nest in ("run", "index", "execution", "config"):
        sub = data.get(nest)
        if isinstance(sub, Mapping):
            nested_jobs, nested_src = _jobs_from_mapping(sub)
            if nested_src is not None:
                return nested_jobs, f"{nest}.{nested_src}"
    connect = data.get("connect")
    if isinstance(connect, Mapping):
        nested_jobs, nested_src = _jobs_from_mapping(connect)
        if nested_src is not None:
            return nested_jobs, f"connect.{nested_src}"
    return 0, None


def jobs_from_env() -> tuple[int, Optional[str]]:
    raw = os.environ.get("HIERWALK_JOBS", "").strip()
    if not raw:
        return 0, None
    return _parse_jobs(raw), "env:HIERWALK_JOBS"


@dataclass(frozen=True)
class JobsResolution:
    jobs: int
    source: str

    @property
    def note(self) -> str:
        if self.jobs == 0:
            return "auto"
        return str(self.jobs)


def _parse_string_list(data: Any, *, field: str) -> List[str]:
    if data is None:
        return []
    if isinstance(data, str):
        return [part.strip() for part in data.split(",") if part.strip()]
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    raise ValueError(f"'{field}' must be a string or array")


def _parse_check_connect(data: Any) -> Optional[Tuple[str, str]]:
    if data is None:
        return None
    if isinstance(data, (list, tuple)):
        if len(data) != 2:
            raise ValueError("'check_connect' must have exactly two endpoints")
        return str(data[0]).strip(), str(data[1]).strip()
    if isinstance(data, Mapping):
        for a_key, b_key in (("a", "b"), ("from", "to"), ("endpoint_a", "endpoint_b")):
            if a_key in data and b_key in data:
                return str(data[a_key]).strip(), str(data[b_key]).strip()
        raise ValueError("'check_connect' object needs a/b or from/to")
    raise ValueError("'check_connect' must be [a, b] or an object")


def normalize_run_mode(mode: str) -> str:
    return str(mode or "").strip().replace("_", "-")


_INDEX_STRATEGY_MODES = frozenset({"full-index", "path-walk"})
_LEGACY_INDEX_STRATEGY = {
    "check-connect": "full-index",
    "check-connect-batch": "full-index",
    "inst-trace": "full-index",
    "inst_trace": "full-index",
    "cone": "full-index",
    "fanin-cone": "full-index",
    "fanout-cone": "full-index",
    "hierarchy": "full-index",
    "full": "full-index",
    "full-index": "full-index",
    "path-walk": "path-walk",
}
_PATH_WALK_DEFAULT_MODES = frozenset(
    {
        "check-connect",
        "check-connect-batch",
        "path-walk",
        "inst-trace",
        "cone",
    }
)


def normalize_index_strategy(value: str) -> str:
    """Map explicit index strategy (or legacy alias) to ``full-index`` or ``path-walk``."""
    key = normalize_run_mode(str(value or ""))
    if not key:
        return ""
    mapped = _LEGACY_INDEX_STRATEGY.get(key, key)
    if mapped in _INDEX_STRATEGY_MODES:
        return mapped
    raise ValueError(
        f"unknown index_strategy {value!r}; expected full-index or path-walk "
        f"(legacy aliases: check-connect-batch, inst-trace, fanout-cone, …)"
    )


def resolve_effective_index_strategy(
    cfg: RunConfig,
    effective_mode: str,
) -> str:
    """
    Resolve index/elab strategy for one run.

    Verification modes (connect, inst-trace, cone) default to path-walk so RTL is
    preprocessed on-demand along endpoint hierarchy paths, not across the full filelist.
    Set ``index_strategy: full-index`` to force the legacy whole-design index.

    ``connect_phase: hgrep`` uses ``hgrep`` strategy — no path-walk index, only
    hierarchy_grep gate + ``grep_hie.json`` cache.
    """
    phase = (cfg.verification_phase or "").strip().lower()
    if phase == "hgrep" or effective_mode == "check-hgrep":
        return "hgrep"
    explicit = normalize_index_strategy(cfg.index_strategy)
    if explicit:
        return explicit
    mode = normalize_run_mode(effective_mode or "")
    if mode == "path-walk" or mode in _PATH_WALK_DEFAULT_MODES:
        return "path-walk"
    return "full-index"


def _document_has_key(data: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        if _mapping_get_ci(data, key) is not None:
            return True
    return False


def resolve_effective_run_mode(
    cfg: RunConfig,
    connect_request: Optional[ConnectivityRequest] = None,
) -> str:
    """Resolve the run mode after config + batch JSON + CLI merges."""
    explicit = normalize_run_mode(cfg.mode or "")
    if explicit:
        return explicit
    if cfg.find_top:
        return "find-top"
    if cfg.inst_trace is not None:
        return "inst-trace"
    if cfg.fanin_cone or cfg.fanout_cone:
        return "cone"
    if effective_search_spec(cfg) is not None:
        return "search"
    if cfg.check_connect:
        return "check-connect"
    if cfg.check_hgrep:
        return "check-hgrep"
    if connect_request is not None:
        return "check-connect-batch"
    return "hierarchy"


def _explicit_mode_from_document(data: Mapping[str, Any]) -> str:
    """Read an explicit ``mode`` from run/connect JSON (top-level or under ``connect``)."""
    raw = _mapping_get_ci(data, "mode")
    if raw is None:
        connect = data.get("connect")
        if isinstance(connect, Mapping):
            raw = _mapping_get_ci(connect, "mode")
    return normalize_run_mode(str(raw or ""))


def _infer_mode(data: Mapping[str, Any]) -> str:
    explicit = _explicit_mode_from_document(data)
    if explicit:
        return explicit
    if data.get("find_top"):
        return "find-top"
    if data.get("check_connect") is not None:
        return "check-connect"
    if data.get("check_connect_batch") is not None or data.get("connect") is not None:
        return "check-connect-batch"
    if (
        data.get("inst_trace") is not None
        or data.get("inst-trace") is not None
    ):
        return "inst-trace"
    if (
        data.get("fanin_cone") is not None
        or data.get("fanin-cone") is not None
        or data.get("fanout_cone") is not None
        or data.get("fanout-cone") is not None
    ):
        return "cone"
    if document_has_search(data):
        return "search"
    return "hierarchy"


def _validate_mode(data: Mapping[str, Any], mode: str) -> None:
    allowed = {
        "hierarchy",
        "find-top",
        "search",
        "check-connect",
        "check-connect-batch",
        "cone",
        "inst-trace",
        "path-walk",
    }
    if mode not in allowed:
        raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(allowed)}")

    flags = {
        "find-top": bool(data.get("find_top")),
        "check-connect": data.get("check_connect") is not None,
        "check-connect-batch": (
            data.get("check_connect_batch") is not None or data.get("connect") is not None
        ),
        "search": document_has_search(data),
        "cone": (
            data.get("fanin_cone") is not None
            or data.get("fanin-cone") is not None
            or data.get("fanout_cone") is not None
            or data.get("fanout-cone") is not None
        ),
    }
    if mode == "hierarchy":
        if any(flags.values()):
            pass
        return
    if mode == "find-top" and not flags["find-top"]:
        data = dict(data)
        data["find_top"] = True
    if mode == "check-connect" and not flags["check-connect"]:
        raise ValueError("mode check-connect requires 'check_connect'")
    if mode == "check-connect-batch" and not flags["check-connect-batch"]:
        raise ValueError(
            "mode check-connect-batch requires 'check_connect_batch' or 'connect'"
        )
    if mode == "search" and not flags["search"]:
        raise ValueError("mode search requires 'search' and/or 'search_path'")
    if mode == "cone" and not flags["cone"]:
        raise ValueError("mode cone requires 'fanin_cone' and/or 'fanout_cone'")
    if mode == "inst-trace" and not (
        data.get("inst_trace") is not None or data.get("inst-trace") is not None
    ):
        raise ValueError("mode inst-trace requires 'inst_trace'")
    if mode == "path-walk" and not (
        flags["check-connect"] or flags["check-connect-batch"]
    ):
        raise ValueError(
            "mode path-walk requires 'check_connect' or 'connect' / 'check_connect_batch'"
        )
    fanin_ep = data.get("fanin_cone", data.get("fanin-cone"))
    fanout_ep = data.get("fanout_cone", data.get("fanout-cone"))
    if fanin_ep and fanout_ep:
        raise ValueError("use either 'fanin_cone' or 'fanout_cone', not both")


def parse_run_request_json(
    data: Any,
    *,
    base_dir: Optional[Path] = None,
) -> RunConfig:
    """
    Parse a full hier-walk run JSON document.

    Example::

        {
          "filelist": "filelist.f",
          "top": "stress_top",
          "mode": "check-connect-batch",
          "output": "connect.tsv",
          "no_cache": true,
          "defines": {"STRESS_USE_IN": "1"},
          "include_ff": true,
          "connect": {
            "checks": [{"id": "clk", "a": "top.clk", "b": "top.u0.clk"}]
          }
        }
    """
    if not isinstance(data, Mapping):
        raise ValueError("run request JSON must be an object")

    base = base_dir or Path.cwd()
    filelist = str(data.get("filelist") or "").strip()
    if not filelist:
        raise ValueError("run request needs 'filelist'")

    mode = _infer_mode(data)
    _validate_mode(data, mode)

    defines = _parse_defines(data.get("defines"))
    max_depth = data.get("max_depth")
    if max_depth is not None:
        max_depth = int(max_depth)

    connect_inline: Optional[Any] = None
    check_connect_batch: Optional[str] = None
    connect_batch_raw = data.get("check_connect_batch")
    connect_raw = data.get("connect")
    if connect_batch_raw is not None:
        if isinstance(connect_batch_raw, (dict, list)):
            connect_inline = connect_batch_raw
        else:
            check_connect_batch = _resolve_path(base, str(connect_batch_raw).strip())
    if connect_raw is not None:
        if connect_inline is not None:
            raise ValueError("use either 'connect' or 'check_connect_batch', not both")
        connect_inline = connect_raw

    over_approx = data.get("over_approximate_if")
    if over_approx is not None and not isinstance(over_approx, bool):
        raise ValueError("'over_approximate_if' must be boolean or null")

    include_ff = bool(data.get("include_ff", False))
    if "ff_barrier" in data:
        include_ff = not bool(data["ff_barrier"])

    fanin_ep = data.get("fanin_cone", data.get("fanin-cone"))
    fanout_ep = data.get("fanout_cone", data.get("fanout-cone"))
    inst_trace_raw = data.get("inst_trace", data.get("inst-trace"))
    inst_trace_req: Optional[InstTraceRequest] = None
    if inst_trace_raw is not None:
        inst_trace_req = parse_inst_trace_json(
            inst_trace_raw,
            top=str(data.get("top") or "").strip(),
            defines=defines,
        )

    trace_stop = parse_trace_stop_policy(data)

    index_strategy = ""
    index_raw = _mapping_get_ci(data, "index_strategy")
    if index_raw is None:
        index_raw = _mapping_get_ci(data, "index-strategy")
    if index_raw is not None and str(index_raw).strip():
        index_strategy = normalize_index_strategy(str(index_raw))

    raw_search = _mapping_get_ci(data, "search")
    search_spec = resolve_search_spec(data)
    search: Optional[str] = None
    search_path: Optional[str] = None
    search_case_insensitive = bool(
        _mapping_get_ci(data, "search_case_insensitive")
        or _mapping_get_ci(data, "search-case-insensitive")
        or False
    )
    if search_spec is not None and search_spec.case_insensitive:
        search_case_insensitive = True
    if not isinstance(raw_search, Mapping):
        search = str(raw_search or "").strip() or None
        search_path = (
            str(
                _mapping_get_ci(data, "search_path")
                or _mapping_get_ci(data, "search-path")
                or ""
            ).strip()
            or None
        )

    return RunConfig(
        filelist=_resolve_path(base, filelist) or filelist,
        top=str(data.get("top") or "").strip() or None,
        find_top=bool(data.get("find_top")) or mode == "find-top",
        all_tops=bool(data.get("all_tops", False)),
        output=_resolve_path(base, str(data.get("output") or "-")) or "-",
        index_cwd=_resolve_path(base, data.get("index_cwd")),
        defines=tuple(defines.items()),
        max_depth=max_depth,
        search=search,
        search_subtree=bool(data.get("search_subtree", True)),
        search_path=search_path,
        search_module=bool(data.get("search_module", False)),
        search_case_insensitive=search_case_insensitive,
        search_spec=search_spec,
        check_connect=_parse_check_connect(data.get("check_connect")),
        check_connect_batch=check_connect_batch,
        connect_inline=connect_inline,
        connect_trace=bool(data.get("connect_trace", data.get("trace", False))),
        connect_log=bool(data.get("connect_log", False)),
        include_ff=include_ff,
        fanin_cone=str(fanin_ep or "").strip() or None,
        fanout_cone=str(fanout_ep or "").strip() or None,
        cone_graph=_resolve_path(base, data.get("cone_graph", data.get("cone-graph"))),
        inst_trace=inst_trace_req,
        strict_generate=bool(data.get("strict_generate", False)),
        over_approximate_if=over_approx,
        ignore_path=tuple(
            _parse_string_list(
                data.get("ignore_path", data.get("ignore-path")),
                field="ignore_path",
            )
        ),
        ignore_path_file=tuple(
            _resolve_path(base, p) or p
            for p in _parse_string_list(
                data.get("ignore_path_file", data.get("ignore-path-file")),
                field="ignore_path_file",
            )
        ),
        ignore_module=tuple(
            _parse_string_list(
                data.get("ignore_module", data.get("ignore-module")),
                field="ignore_module",
            )
        ),
        ignore_filelist=tuple(
            _parse_string_list(
                data.get("ignore_filelist", data.get("ignore-filelist")),
                field="ignore_filelist",
            )
        ),
        ignore_hierarchy=trace_stop.ignore_hierarchy,
        trace_max_depth=trace_stop.trace_max_depth,
        jobs=_jobs_from_document(data)[0],
        low_memory=bool(data.get("low_memory", False)),
        cache_dir=_resolve_path(base, data.get("cache_dir")),
        no_cache=bool(data.get("no_cache", False)),
        refresh_cache=bool(data.get("refresh_cache", False)),
        quiet=bool(data.get("quiet", False)),
        log_file=_resolve_path(base, data.get("log_file")),
        no_log_file=bool(data.get("no_log_file", False)),
        mode=mode,
        index_strategy=index_strategy,
    )


def parse_shared_run_request_json(
    data: Any,
    *,
    base_dir: Optional[Path] = None,
) -> RunConfig:
    """
    Parse shared run fields from a JSON document.

    Flat/legacy suite JSON keeps only top-level ``filelist``, ``top``, ``defines``,
    etc. Step blocks (``run_on_full_index``, ``run_conn_check``, …) and their
    ``mode``/``enable`` do not set a global execution mode.
    """
    if not isinstance(data, Mapping):
        raise ValueError("run request JSON must be an object")

    parse_data: Mapping[str, Any] = (
        strip_run_suite_blocks(data) if is_run_test_suite_document(data) else data
    )
    cfg = parse_run_request_json(parse_data, base_dir=base_dir)
    if not is_run_test_suite_document(data):
        return cfg
    return replace(
        cfg,
        mode=None,
        check_connect=None,
        check_connect_batch=None,
        connect_inline=None,
        inst_trace=None,
        fanin_cone=None,
        fanout_cone=None,
        ignore_path=(),
        ignore_path_file=(),
        ignore_module=(),
        ignore_filelist=(),
        jobs=_jobs_from_document(data)[0],
    )


def load_run_request(path: Union[str, Path]) -> RunConfig:
    p = Path(path)
    data = read_json_document(p)
    if isinstance(data, Mapping):
        apply_config_env_from_document(data)
    return parse_shared_run_request_json(data, base_dir=p.parent)


def _apply_run_document_fields(
    cfg: RunConfig,
    data: Mapping[str, Any],
    *,
    base_dir: Path,
    args: Any,
    jobs_source_prefix: str = "connect-batch",
) -> tuple[RunConfig, Optional[str]]:
    """Merge run-level JSON fields into ``cfg`` (batch JSON has run JSON parity)."""
    out = cfg
    jobs_source: Optional[str] = None

    if _document_has_key(data, "filelist") and not args.filelist:
        fl_raw = str(_mapping_get_ci(data, "filelist") or "").strip()
        if fl_raw:
            resolved = _resolve_path(base_dir, fl_raw)
            if resolved:
                out = replace(out, filelist=resolved)

    if _document_has_key(data, "output") and not _field_overridden(args, "output", "-"):
        out_raw = _mapping_get_ci(data, "output")
        if out_raw is not None and str(out_raw).strip():
            out = replace(
                out,
                output=_resolve_path(base_dir, str(out_raw).strip()) or "-",
            )

    if not _field_overridden(args, "mode", None):
        batch_mode = _explicit_mode_from_document(data)
        if batch_mode:
            out = replace(out, mode=batch_mode)
        elif bool(_mapping_get_ci(data, "find_top")):
            out = replace(out, mode="find-top", find_top=True)

    index_raw = _mapping_get_ci(data, "index_strategy")
    if index_raw is None:
        index_raw = _mapping_get_ci(data, "index-strategy")
    if index_raw is not None and str(index_raw).strip():
        out = replace(
            out,
            index_strategy=normalize_index_strategy(str(index_raw)),
        )

    if _document_has_key(data, "defines") and not args.define:
        batch_defines = _parse_defines(_mapping_get_ci(data, "defines"))
        if batch_defines:
            merged = dict(out.defines_map)
            merged.update(batch_defines)
            out = replace(out, defines=tuple(merged.items()))

    if _document_has_key(data, "index_cwd", "index-cwd") and not _field_overridden(
        args, "index_cwd", None
    ):
        cwd_raw = _mapping_get_ci(data, "index_cwd") or _mapping_get_ci(data, "index-cwd")
        if cwd_raw:
            out = replace(out, index_cwd=_resolve_path(base_dir, str(cwd_raw)))

    if not _field_overridden(args, "jobs", 0):
        jobs, src = _jobs_from_document(data)
        if src is not None:
            out = replace(out, jobs=jobs)
            jobs_source = f"{jobs_source_prefix}:{src}"

    if _document_has_key(data, "top") and not _field_overridden(args, "top", None):
        top = str(_mapping_get_ci(data, "top") or "").strip()
        if top:
            out = replace(out, top=top)

    if _document_has_key(data, "find_top") and not args.find_top:
        if bool(_mapping_get_ci(data, "find_top")):
            out = replace(out, find_top=True)

    if _document_has_key(data, "all_tops") and not args.all_tops:
        if bool(_mapping_get_ci(data, "all_tops")):
            out = replace(out, all_tops=True)

    if _document_has_key(data, "max_depth") and not _field_overridden(args, "max_depth", None):
        raw_depth = _mapping_get_ci(data, "max_depth")
        if raw_depth is not None:
            out = replace(out, max_depth=int(raw_depth))

    search_keys = (
        "search",
        "search_path",
        "search-path",
        "search_subtree",
        "search-subtree",
        "search_module",
        "search-module",
        "search_case_insensitive",
        "search-case-insensitive",
    )
    if any(_document_has_key(data, key) for key in search_keys):
        raw_search = _mapping_get_ci(data, "search")
        if isinstance(raw_search, Mapping) and not _field_overridden(
            args, "search", None
        ):
            spec = resolve_search_spec(data)
            if spec is not None:
                out = replace(
                    out,
                    search_spec=spec,
                    search=None,
                    search_path=None,
                    search_case_insensitive=spec.case_insensitive,
                )
        else:
            if _document_has_key(data, "search") and not _field_overridden(
                args, "search", None
            ):
                search = str(raw_search or "").strip()
                if search:
                    out = replace(out, search=search, search_spec=None)
            if _document_has_key(data, "search_path", "search-path") and not (
                _field_overridden(args, "search_path", None)
            ):
                search_path = str(
                    _mapping_get_ci(data, "search_path")
                    or _mapping_get_ci(data, "search-path")
                    or ""
                ).strip()
                if search_path:
                    out = replace(out, search_path=search_path, search_spec=None)

    if _document_has_key(data, "search_subtree", "search-subtree") and not (
        args.search_subtree
    ):
        if bool(
            _mapping_get_ci(data, "search_subtree")
            or _mapping_get_ci(data, "search-subtree")
        ):
            out = replace(out, search_subtree=True)

    if _document_has_key(data, "search_module", "search-module") and not (
        args.search_module
    ):
        if bool(
            _mapping_get_ci(data, "search_module")
            or _mapping_get_ci(data, "search-module")
        ):
            out = replace(out, search_module=True)

    if _document_has_key(
        data, "search_case_insensitive", "search-case-insensitive"
    ) and not getattr(args, "search_case_insensitive", False):
        if bool(
            _mapping_get_ci(data, "search_case_insensitive")
            or _mapping_get_ci(data, "search-case-insensitive")
        ):
            out = replace(out, search_case_insensitive=True)

    if _document_has_key(data, "check_connect") and not args.check_connect:
        parsed = _parse_check_connect(_mapping_get_ci(data, "check_connect"))
        if parsed is not None:
            out = replace(out, check_connect=parsed)

    if _document_has_key(data, "connect_trace", "trace") and not args.connect_trace:
        if bool(
            _mapping_get_ci(data, "connect_trace")
            or _mapping_get_ci(data, "trace")
        ):
            out = replace(out, connect_trace=True)

    if _document_has_key(data, "connect_log") and not getattr(args, "connect_log", False):
        if bool(_mapping_get_ci(data, "connect_log")):
            out = replace(out, connect_log=True)

    if _document_has_key(data, "include_ff") and not args.include_ff:
        if bool(_mapping_get_ci(data, "include_ff")):
            out = replace(out, include_ff=True)
    if _document_has_key(data, "ff_barrier") and not args.include_ff:
        out = replace(out, include_ff=not bool(_mapping_get_ci(data, "ff_barrier")))

    if _document_has_key(data, "over_approximate_if"):
        over_approx = _mapping_get_ci(data, "over_approximate_if")
        if over_approx is None or isinstance(over_approx, bool):
            out = replace(out, over_approximate_if=over_approx)

    if _document_has_key(data, "strict_generate") and not getattr(
        args, "strict_generate", False
    ):
        if bool(_mapping_get_ci(data, "strict_generate")):
            out = replace(out, strict_generate=True)

    if _document_has_key(data, "ignore_path", "ignore-path") and not args.ignore_path:
        ignore_raw = _mapping_get_ci(data, "ignore_path") or _mapping_get_ci(
            data, "ignore-path"
        )
        if ignore_raw is not None:
            out = replace(
                out,
                ignore_path=tuple(_parse_string_list(ignore_raw, field="ignore_path")),
            )

    if _document_has_key(data, "ignore_path_file", "ignore-path-file") and not args.ignore_path_file:
        ignore_raw = _mapping_get_ci(data, "ignore_path_file") or _mapping_get_ci(
            data, "ignore-path-file"
        )
        if ignore_raw is not None:
            out = replace(
                out,
                ignore_path_file=tuple(
                    _resolve_path(base_dir, p) or p
                    for p in _parse_string_list(
                        ignore_raw,
                        field="ignore_path_file",
                    )
                ),
            )

    if _document_has_key(data, "ignore_module", "ignore-module") and not args.ignore_module:
        ignore_raw = _mapping_get_ci(data, "ignore_module") or _mapping_get_ci(
            data, "ignore-module"
        )
        if ignore_raw is not None:
            out = replace(
                out,
                ignore_module=tuple(
                    _parse_string_list(ignore_raw, field="ignore_module")
                ),
            )

    if _document_has_key(data, "ignore_filelist", "ignore-filelist") and not getattr(
        args, "ignore_filelist", None
    ):
        ignore_raw = _mapping_get_ci(data, "ignore_filelist") or _mapping_get_ci(
            data, "ignore-filelist"
        )
        if ignore_raw is not None:
            out = replace(
                out,
                ignore_filelist=tuple(
                    _parse_string_list(ignore_raw, field="ignore_filelist")
                ),
            )

    if _document_has_key(data, "no_cache") and not args.no_cache:
        if bool(_mapping_get_ci(data, "no_cache")):
            out = replace(out, no_cache=True)

    if _document_has_key(data, "refresh_cache") and not args.refresh_cache:
        if bool(_mapping_get_ci(data, "refresh_cache")):
            out = replace(out, refresh_cache=True)

    if _document_has_key(data, "low_memory") and not getattr(args, "low_memory", False):
        if bool(_mapping_get_ci(data, "low_memory")):
            out = replace(out, low_memory=True)

    if _document_has_key(data, "cache_dir") and not _field_overridden(args, "cache_dir", None):
        cache_dir = _mapping_get_ci(data, "cache_dir")
        if cache_dir:
            out = replace(out, cache_dir=_resolve_path(base_dir, str(cache_dir)))

    if _document_has_key(data, "quiet") and not args.quiet:
        if bool(_mapping_get_ci(data, "quiet")):
            out = replace(out, quiet=True)

    if _document_has_key(data, "log_file") and not _field_overridden(args, "log_file", None):
        log_file = _mapping_get_ci(data, "log_file")
        if log_file:
            out = replace(out, log_file=_resolve_path(base_dir, str(log_file)))

    if _document_has_key(data, "no_log_file") and not args.no_log_file:
        if bool(_mapping_get_ci(data, "no_log_file")):
            out = replace(out, no_log_file=True)

    if _document_has_key(data, "inst_trace", "inst-trace"):
        inst_raw = _mapping_get_ci(data, "inst_trace") or _mapping_get_ci(
            data, "inst-trace"
        )
        if inst_raw is not None:
            out = replace(
                out,
                inst_trace=parse_inst_trace_json(
                    inst_raw,
                    top=out.top or str(_mapping_get_ci(data, "top") or "").strip(),
                    defines=dict(out.defines_map),
                ),
            )

    if _document_has_key(data, "fanin_cone", "fanin-cone") and not getattr(
        args, "fanin_cone", None
    ):
        fanin = _mapping_get_ci(data, "fanin_cone") or _mapping_get_ci(data, "fanin-cone")
        if fanin:
            out = replace(out, fanin_cone=str(fanin).strip())

    if _document_has_key(data, "fanout_cone", "fanout-cone") and not getattr(
        args, "fanout_cone", None
    ):
        fanout = _mapping_get_ci(data, "fanout_cone") or _mapping_get_ci(
            data, "fanout-cone"
        )
        if fanout:
            out = replace(out, fanout_cone=str(fanout).strip())

    if _document_has_key(data, "cone_graph", "cone-graph") and not getattr(
        args, "cone_graph", None
    ):
        graph = _mapping_get_ci(data, "cone_graph") or _mapping_get_ci(data, "cone-graph")
        if graph:
            out = replace(out, cone_graph=_resolve_path(base_dir, str(graph)))

    phase_keys = (
        "connect_phase",
        "connect-phase",
        "verification_phase",
        "verification-phase",
        "phase",
    )
    if any(_document_has_key(data, key) for key in phase_keys):
        raw = None
        for key in phase_keys:
            hit = _mapping_get_ci(data, key)
            if hit is not None and str(hit).strip():
                raw = hit
                break
        if raw is not None:
            out = replace(out, verification_phase=parse_connect_phase_value(raw))

    return out, jobs_source


def merge_options_from_connect_batch_json(
    cfg: RunConfig,
    batch_path: Union[str, Path],
    args: Any,
) -> tuple[RunConfig, Optional[str], List[str], Optional[dict]]:
    """
    Apply run-level fields from ``--check-connect-batch`` JSON.

    Batch JSON has the same run-level field surface as run JSON.
    """
    if not batch_path:
        return cfg, None, [], None
    p = Path(batch_path)
    if not p.is_file():
        return cfg, None, [], None
    try:
        data = read_json_document(p)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return cfg, None, [], None
    if not isinstance(data, Mapping):
        return cfg, None, [], None

    json_env_applied = apply_config_env_from_document(data)
    merged, jobs_src = _apply_run_document_fields(
        cfg,
        data,
        base_dir=p.parent,
        args=args,
    )
    return merged, jobs_src, json_env_applied, dict(data)


def resolve_jobs_after_merge(
    cfg: RunConfig,
    args: Any,
    *,
    json_jobs_source: Optional[str] = None,
    connect_batch_jobs_source: Optional[str] = None,
    env_jobs_source: Optional[str] = None,
) -> JobsResolution:
    """Describe where effective parallel job count came from."""
    if _field_overridden(args, "jobs", 0):
        return JobsResolution(jobs=cfg.jobs, source="cli:-j")
    if env_jobs_source is not None:
        return JobsResolution(jobs=cfg.jobs, source=env_jobs_source)
    if connect_batch_jobs_source is not None:
        return JobsResolution(jobs=cfg.jobs, source=connect_batch_jobs_source)
    if json_jobs_source is not None:
        return JobsResolution(jobs=cfg.jobs, source=f"json:{json_jobs_source}")
    if cfg.jobs == 0:
        return JobsResolution(jobs=0, source="default:auto")
    return JobsResolution(jobs=cfg.jobs, source="json")


def jobs_hint_from_config_text(text: str) -> Optional[str]:
    """Best-effort detect jobs-like keys in raw JSON when parsing yielded auto."""
    lowered = text.lower()
    for token in _JOBS_KEY_ALIASES:
        if f'"{token}"' in lowered or f"'{token}'" in lowered:
            return token
    return None


def try_load_run_request_from_path(
    path: Union[str, Path],
) -> Optional[Tuple[Path, RunConfig, Optional[str]]]:
    """
    If *path* is a run-spec JSON (object with ``filelist``), return ``(path, cfg)``.

    Used when users pass ``hier-walk run.json`` as the positional argument.
    """
    p = Path(path)
    if p.suffix.lower() != ".json" or not p.is_file():
        return None
    try:
        data = read_json_document(p)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, Mapping) or "filelist" not in data:
        return None
    try:
        apply_config_env_from_document(data)
        cfg = parse_shared_run_request_json(data, base_dir=p.parent)
        _, src = _jobs_from_document(data)
        return p, cfg, src
    except ValueError:
        return None


def load_run_request_with_jobs_source(
    path: Union[str, Path],
) -> tuple[RunConfig, Optional[str]]:
    p = Path(path)
    data = read_json_document(p)
    if isinstance(data, Mapping):
        apply_config_env_from_document(data)
    cfg = parse_shared_run_request_json(data, base_dir=p.parent)
    _, src = _jobs_from_document(data)
    return cfg, src


def run_config_to_json(cfg: RunConfig, *, indent: int = 2) -> str:
    payload: Dict[str, Any] = {
        "filelist": cfg.filelist,
        "output": cfg.output,
        "defines": dict(cfg.defines),
        "jobs": cfg.jobs,
        "low_memory": cfg.low_memory,
        "no_cache": cfg.no_cache,
        "refresh_cache": cfg.refresh_cache,
        "quiet": cfg.quiet,
        "no_log_file": cfg.no_log_file,
    }
    if cfg.top:
        payload["top"] = cfg.top
    if cfg.find_top:
        payload["mode"] = "find-top"
        payload["find_top"] = True
    elif cfg.check_connect:
        payload["mode"] = "check-connect"
        payload["check_connect"] = list(cfg.check_connect)
    elif cfg.connect_inline is not None or cfg.check_connect_batch:
        payload["mode"] = "check-connect-batch"
    elif cfg.fanin_cone or cfg.fanout_cone:
        payload["mode"] = "cone"
    elif effective_search_spec(cfg) is not None:
        payload["mode"] = "search"
    else:
        payload["mode"] = "hierarchy"

    if cfg.all_tops:
        payload["all_tops"] = True
    if cfg.index_cwd:
        payload["index_cwd"] = cfg.index_cwd
    if cfg.max_depth is not None:
        payload["max_depth"] = cfg.max_depth
    if cfg.search:
        payload["search"] = cfg.search
    if cfg.search_subtree:
        payload["search_subtree"] = True
    if cfg.search_path:
        payload["search_path"] = cfg.search_path
    if cfg.search_module:
        payload["search_module"] = True
    if cfg.search_case_insensitive:
        payload["search_case_insensitive"] = True
    if cfg.connect_trace:
        payload["connect_trace"] = True
    if cfg.connect_log:
        payload["connect_log"] = True
    if cfg.include_ff:
        payload["include_ff"] = True
    if cfg.fanin_cone:
        payload["fanin_cone"] = cfg.fanin_cone
    if cfg.fanout_cone:
        payload["fanout_cone"] = cfg.fanout_cone
    if cfg.cone_graph:
        payload["cone_graph"] = cfg.cone_graph
    if cfg.strict_generate:
        payload["strict_generate"] = True
    if cfg.over_approximate_if is not None:
        payload["over_approximate_if"] = cfg.over_approximate_if
    if cfg.ignore_path:
        payload["ignore_path"] = list(cfg.ignore_path)
    if cfg.ignore_path_file:
        payload["ignore_path_file"] = list(cfg.ignore_path_file)
    if cfg.ignore_module:
        payload["ignore_module"] = list(cfg.ignore_module)
    if cfg.ignore_filelist:
        payload["ignore_filelist"] = list(cfg.ignore_filelist)
    if cfg.cache_dir:
        payload["cache_dir"] = cfg.cache_dir
    if cfg.log_file:
        payload["log_file"] = cfg.log_file
    if cfg.check_connect_batch:
        payload["check_connect_batch"] = cfg.check_connect_batch
    if cfg.connect_inline is not None:
        payload["connect"] = cfg.connect_inline
    return json.dumps(payload, indent=indent) + "\n"


def write_run_request(path: Union[str, Path], cfg: RunConfig) -> None:
    Path(path).write_text(run_config_to_json(cfg), encoding="utf-8")


def _connect_payload_for_merge(cfg: RunConfig) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if cfg.top:
        payload["top"] = cfg.top
    if cfg.defines:
        payload["defines"] = dict(cfg.defines)
    if cfg.connect_trace:
        payload["connect_trace"] = True
    if cfg.connect_log:
        payload["connect_log"] = True
    if cfg.include_ff:
        payload["include_ff"] = True
    if cfg.strict_generate:
        payload["strict_generate"] = True
    if cfg.over_approximate_if is not None:
        payload["over_approximate_if"] = cfg.over_approximate_if
    return payload


def resolve_connectivity_request(cfg: RunConfig) -> Optional[ConnectivityRequest]:
    """Build connectivity request from inline JSON, external file, or None."""
    if cfg.connect_inline is not None:
        inline = cfg.connect_inline
        if isinstance(inline, Mapping):
            merged = dict(_connect_payload_for_merge(cfg))
            merged.update(inline)
            req = parse_connect_request_json(merged)
        else:
            req = parse_connect_request_json(inline)
            req = _merge_connect_run_options(req, cfg)
        return req
    batch_path = cfg.check_hgrep or cfg.check_connect_batch
    if batch_path:
        p = Path(batch_path)
        text = p.read_text(encoding="utf-8-sig").lstrip()
        if p.suffix.lower() == ".json" or text.startswith(("{", "[")):
            data = json.loads(text)
            req = try_parse_connect_request_json(data)
            if req is not None:
                return _merge_connect_run_options(req, cfg)
            return None
        req = load_connect_request(str(batch_path))
        return _merge_connect_run_options(req, cfg)
    return None


def _merge_connect_run_options(
    req: ConnectivityRequest,
    cfg: RunConfig,
) -> ConnectivityRequest:
    top = cfg.top or req.top
    defines = dict(req.defines)
    defines.update(cfg.defines_map)
    trace = req.trace or cfg.connect_trace or cfg.connect_log
    include_ff = req.include_ff or cfg.include_ff
    strict_generate = req.strict_generate or cfg.strict_generate
    over_approx = (
        cfg.over_approximate_if
        if cfg.over_approximate_if is not None
        else req.over_approximate_if
    )
    if (
        top == req.top
        and defines == req.defines
        and trace == req.trace
        and include_ff == req.include_ff
        and strict_generate == req.strict_generate
        and over_approx == req.over_approximate_if
    ):
        return req
    connect_log = req.connect_log or cfg.connect_log
    return ConnectivityRequest(
        checks=req.checks,
        top=top,
        defines=defines,
        trace=trace,
        connect_log=connect_log,
        include_ff=include_ff,
        strict_generate=strict_generate,
        over_approximate_if=over_approx,
    )


def run_config_from_args(args: Any) -> RunConfig:
    """Build RunConfig from an argparse.Namespace."""
    defines = _parse_defines(getattr(args, "define", []) or [])
    check_connect = None
    if getattr(args, "check_connect", None):
        check_connect = (args.check_connect[0], args.check_connect[1])
    return RunConfig(
        filelist=args.filelist or "",
        top=args.top,
        find_top=bool(args.find_top),
        all_tops=bool(args.all_tops),
        output=args.output,
        index_cwd=args.index_cwd,
        defines=tuple(defines.items()),
        max_depth=args.max_depth,
        search=args.search,
        search_subtree=bool(args.search_subtree),
        search_path=args.search_path,
        search_module=bool(args.search_module),
        search_case_insensitive=bool(
            getattr(args, "search_case_insensitive", False)
        ),
        check_connect=check_connect,
        check_connect_batch=args.check_connect_batch,
        check_hgrep=getattr(args, "check_hgrep", None),
        connect_trace=bool(args.connect_trace),
        connect_log=bool(getattr(args, "connect_log", False)),
        include_ff=bool(args.include_ff),
        fanin_cone=getattr(args, "fanin_cone", None),
        fanout_cone=getattr(args, "fanout_cone", None),
        cone_graph=getattr(args, "cone_graph", None),
        ignore_path=tuple(args.ignore_path or ()),
        ignore_path_file=tuple(args.ignore_path_file or ()),
        ignore_module=tuple(args.ignore_module or ()),
        ignore_filelist=tuple(getattr(args, "ignore_filelist", None) or ()),
        jobs=int(args.jobs),
        low_memory=bool(getattr(args, "low_memory", False)),
        cache_dir=args.cache_dir,
        no_cache=bool(args.no_cache),
        refresh_cache=bool(args.refresh_cache),
        quiet=bool(args.quiet),
        log_file=args.log_file,
        no_log_file=bool(args.no_log_file),
        mode=normalize_run_mode(getattr(args, "mode", "") or "")
        or None,
    )


def _field_overridden(args: Any, name: str, default: Any) -> bool:
    value = getattr(args, name)
    if isinstance(default, list):
        return bool(value)
    return value != default


def merge_run_config(base: RunConfig, cli: RunConfig, args: Any) -> RunConfig:
    """Apply CLI overrides on top of a JSON-loaded RunConfig."""
    out = base
    if args.filelist and try_load_run_request_from_path(args.filelist) is None:
        out = replace(out, filelist=cli.filelist)
    if _field_overridden(args, "top", None):
        out = replace(out, top=cli.top)
    if args.find_top:
        out = replace(out, find_top=True)
    if args.all_tops:
        out = replace(out, all_tops=True)
    if _field_overridden(args, "output", "-"):
        out = replace(out, output=cli.output)
    if _field_overridden(args, "index_cwd", None):
        out = replace(out, index_cwd=cli.index_cwd)
    if args.define:
        merged = dict(out.defines_map)
        merged.update(cli.defines_map)
        out = replace(out, defines=tuple(merged.items()))
    if _field_overridden(args, "max_depth", None):
        out = replace(out, max_depth=cli.max_depth)
    if _field_overridden(args, "search", None):
        out = replace(out, search=cli.search, search_spec=None)
    if _field_overridden(args, "search_subtree", True):
        out = replace(out, search_subtree=cli.search_subtree)
    if _field_overridden(args, "search_path", None):
        out = replace(out, search_path=cli.search_path, search_spec=None)
    if args.search_module:
        out = replace(out, search_module=True)
    if getattr(args, "search_case_insensitive", False):
        out = replace(out, search_case_insensitive=True)
    if args.check_connect:
        out = replace(
            out,
            check_connect=cli.check_connect,
            connect_inline=None,
            check_connect_batch=None,
        )
    if args.check_connect_batch:
        out = replace(
            out,
            check_connect_batch=cli.check_connect_batch,
            connect_inline=None,
            check_connect=cli.check_connect,
            check_hgrep=None,
        )
    if getattr(args, "check_hgrep", None):
        out = replace(
            out,
            check_hgrep=cli.check_hgrep,
            check_connect_batch=None,
            connect_inline=None,
            check_connect=None,
            verification_phase="hgrep",
            mode="path-walk",
            index_strategy="path-walk",
        )
    if args.connect_trace:
        out = replace(out, connect_trace=True)
    if getattr(args, "connect_log", False):
        out = replace(out, connect_log=True)
    if args.include_ff:
        out = replace(out, include_ff=True)
    if getattr(args, "fanin_cone", None):
        out = replace(
            out,
            fanin_cone=cli.fanin_cone,
            fanout_cone=None,
            check_connect=None,
            check_connect_batch=None,
            connect_inline=None,
            search=None,
            search_path=None,
        )
    if getattr(args, "fanout_cone", None):
        out = replace(
            out,
            fanout_cone=cli.fanout_cone,
            fanin_cone=None,
            check_connect=None,
            check_connect_batch=None,
            connect_inline=None,
            search=None,
            search_path=None,
        )
    if getattr(args, "cone_graph", None):
        out = replace(out, cone_graph=cli.cone_graph)
    if args.ignore_path:
        out = replace(out, ignore_path=cli.ignore_path)
    if args.ignore_path_file:
        out = replace(out, ignore_path_file=cli.ignore_path_file)
    if args.ignore_module:
        out = replace(out, ignore_module=cli.ignore_module)
    if getattr(args, "ignore_filelist", None):
        out = replace(out, ignore_filelist=cli.ignore_filelist)
    if getattr(args, "mode", None):
        out = replace(out, mode=cli.mode)
    if _field_overridden(args, "jobs", 0):
        out = replace(out, jobs=cli.jobs)
    if getattr(args, "low_memory", False):
        out = replace(out, low_memory=True)
    if _field_overridden(args, "cache_dir", None):
        out = replace(out, cache_dir=cli.cache_dir)
    if args.no_cache:
        out = replace(out, no_cache=True)
    if args.refresh_cache:
        out = replace(out, refresh_cache=True)
    if args.quiet:
        out = replace(out, quiet=True)
    if _field_overridden(args, "log_file", None):
        out = replace(out, log_file=cli.log_file)
    if args.no_log_file:
        out = replace(out, no_log_file=True)
    return out