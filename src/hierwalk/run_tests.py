"""Flat run JSON: run_on_full_index + run_conn_check / run_io_trace / run_cone_trace."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence, Tuple

from hierwalk.inst_trace import InstTraceRequest, parse_inst_trace_json
from hierwalk.enable_diagnostics import resolve_block_enabled
from hierwalk.run_request import (
    RUN_CONN_CHECK,
    RUN_CONE_TRACE,
    RUN_IO_TRACE,
    RUN_ON_FULL_DB_LEGACY,
    RUN_ON_FULL_INDEX,
    RunConfig,
    _full_index_block_key,
    _mapping_get_ci,
    block_enable_raw,
    block_enabled,
    _parse_check_connect,
    _parse_jobs,
    _parse_string_list,
    _resolve_path,
    normalize_run_mode,
    parse_enable,
    parse_shared_run_request_json,
)

TEST_KINDS: Tuple[str, ...] = (
    RUN_ON_FULL_INDEX,
    RUN_CONN_CHECK,
    RUN_IO_TRACE,
    RUN_CONE_TRACE,
)
VERIFICATION_KINDS: Tuple[str, ...] = (
    RUN_CONN_CHECK,
    RUN_IO_TRACE,
    RUN_CONE_TRACE,
)

_FLAT_BLOCK_ORDER: Tuple[str, ...] = TEST_KINDS

_INDEX_STRATEGY_MODES = frozenset({"full-index", "path-walk"})
_FULL_DB_MODES = frozenset({"hierarchy", "search", "find-top"})

# Legacy verification-type modes → index strategy (backward compatible).
_LEGACY_INDEX_MODE = {
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

_DEFAULT_INDEX_MODE = {
    RUN_CONN_CHECK: "path-walk",
    RUN_IO_TRACE: "path-walk",
    RUN_CONE_TRACE: "path-walk",
}


@dataclass(frozen=True)
class RunTestEntry:
    """One enabled step in a flat or legacy multi-test run JSON."""

    kind: str
    mode: str
    enabled: bool = True
    name: str = ""
    index: int = 0


@dataclass(frozen=True)
class RunTestSuite:
    """Shared run options, optional full-db settings, and ordered enabled steps."""

    shared: RunConfig
    tests: Tuple[RunTestEntry, ...]
    full_index_spec: Optional[Mapping[str, Any]] = None
    full_index_enabled: bool = False
    enable_warnings: Tuple[str, ...] = ()


def _first_ci(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        hit = _mapping_get_ci(data, key)
        if hit is not None:
            return hit
    return None


def _spec_block(data: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    raw = _mapping_get_ci(data, kind)
    if raw is None:
        raise ValueError(f"{kind} block missing")
    if not isinstance(raw, Mapping):
        raise ValueError(f"{kind!r} must be an object")
    return raw


def normalize_index_strategy_mode(mode: str) -> str:
    """Map JSON mode to ``full-index`` or ``path-walk`` (index/elab strategy)."""
    key = normalize_run_mode(str(mode or ""))
    if not key:
        return ""
    mapped = _LEGACY_INDEX_MODE.get(key, key)
    if mapped in _INDEX_STRATEGY_MODES:
        return mapped
    raise ValueError(
        f"unknown index mode {mode!r}; expected full-index or path-walk "
        f"(legacy aliases: check-connect-batch, inst-trace, fanout-cone, …)"
    )


def _validate_conn_check_spec(spec: Mapping[str, Any], *, label: str) -> None:
    checks = _mapping_get_ci(spec, "checks")
    check_connect = _first_ci(spec, "check_connect", "check-connect")
    if checks is None and check_connect is None:
        raise ValueError(f"{label} run_conn_check requires checks")


def _validate_io_trace_spec(spec: Mapping[str, Any], *, label: str) -> None:
    instance = _first_ci(spec, "instance", "inst", "path")
    if not str(instance or "").strip():
        raise ValueError(f"{label} run_io_trace requires instance (hierarchy path)")


def _validate_cone_trace_spec(spec: Mapping[str, Any], *, label: str) -> None:
    fanin = _first_ci(spec, "fanin_cone", "fanin-cone", "endpoint")
    fanout = _first_ci(spec, "fanout_cone", "fanout-cone")
    if fanin and fanout:
        raise ValueError(
            f"{label} run_cone_trace: use fanin_cone or fanout_cone, not both"
        )
    if not fanin and not fanout:
        raise ValueError(
            f"{label} run_cone_trace requires fanin_cone or fanout_cone"
        )


def _validate_verification_index_mode(
    kind: str,
    mode: str,
    spec: Mapping[str, Any],
    *,
    label: str,
) -> str:
    if kind == RUN_CONN_CHECK:
        _validate_conn_check_spec(spec, label=label)
    elif kind == RUN_IO_TRACE:
        _validate_io_trace_spec(spec, label=label)
    else:
        _validate_cone_trace_spec(spec, label=label)
    return mode


def exec_mode_for_verification(kind: str, index_mode: str) -> str:
    """Map flat-suite block kind to CLI execution mode (not index strategy)."""
    if kind == RUN_CONN_CHECK:
        return "check-connect-batch"
    if kind == RUN_IO_TRACE:
        return "inst-trace"
    return "cone"


def _full_index_enable_state(
    data: Mapping[str, Any],
    *,
    raw_text: Optional[str] = None,
) -> tuple[Optional[Mapping[str, Any]], bool, Tuple[str, ...]]:
    """Return ``(spec, enabled, warnings)`` for run_on_full_index / run_on_full_db."""
    key = _full_index_block_key(data)
    if key is None:
        return None, False, ()
    spec = _spec_block(data, key)
    enabled, warnings = resolve_block_enabled(
        spec,
        default=True,
        document=data,
        block_key=key,
        raw_text=raw_text,
    )
    return spec, enabled, warnings


def resolve_verification_index_strategy(
    entry_mode: str,
    *,
    full_index_spec: Optional[Mapping[str, Any]],
    full_index_enabled: bool,
) -> str:
    """
    Apply run_on_full_index.enable to verification index strategy.

    When the full-index block exists with ``enable: 0``, verification steps cannot
    use the full-filelist index strategy even if their block says ``full-index``
    (or a legacy alias such as ``hierarchy`` / ``check-connect-batch``).
    """
    if (
        full_index_spec is not None
        and not full_index_enabled
        and entry_mode == "full-index"
    ):
        return "path-walk"
    return entry_mode


def _validate_full_index_mode(mode: str, spec: Mapping[str, Any], *, label: str) -> str:
    if mode not in _FULL_DB_MODES:
        raise ValueError(
            f"{label} run_on_full_index: unknown mode {mode!r}; "
            f"expected one of {sorted(_FULL_DB_MODES)}"
        )
    from hierwalk.search_spec import document_has_search

    if mode == "search" and not document_has_search(spec):
        raise ValueError(
            f"{label} run_on_full_index mode search requires search/search_path"
        )
    return mode


def _parse_test_entry_from_spec(
    kind: str,
    spec: Mapping[str, Any],
    *,
    label: str,
    index: int,
    enabled: bool,
    name: str = "",
) -> RunTestEntry:
    if kind == RUN_ON_FULL_INDEX:
        raw_mode = _mapping_get_ci(spec, "mode")
        mode = normalize_run_mode(str(raw_mode or ""))
        if not mode:
            mode = "hierarchy"
        mode = _validate_full_index_mode(mode, spec, label=label)
    else:
        raw_mode = _mapping_get_ci(spec, "mode")
        if raw_mode is None or not str(raw_mode).strip():
            mode = _DEFAULT_INDEX_MODE[kind]
        else:
            mode = normalize_index_strategy_mode(str(raw_mode))
        mode = _validate_verification_index_mode(kind, mode, spec, label=label)

    entry_name = str(
        name
        or _mapping_get_ci(spec, "name")
        or _mapping_get_ci(spec, "id")
        or ""
    ).strip()

    return RunTestEntry(
        kind=kind,
        mode=mode,
        enabled=enabled,
        name=entry_name,
        index=index,
    )


def _merge_full_index_fields(
    cfg: RunConfig,
    spec: Mapping[str, Any],
    *,
    base_dir: Any,
) -> RunConfig:
    """Apply run_on_full_index settings (ignores, cache, jobs, …) onto a RunConfig."""
    from pathlib import Path

    base = base_dir or Path.cwd()
    out = cfg

    ignore_raw = _first_ci(spec, "ignore_path", "ignore-path")
    if ignore_raw is not None:
        out = replace(
            out,
            ignore_path=tuple(_parse_string_list(ignore_raw, field="ignore_path")),
        )

    ignore_file_raw = _first_ci(spec, "ignore_path_file", "ignore-path-file")
    if ignore_file_raw is not None:
        out = replace(
            out,
            ignore_path_file=tuple(
                _resolve_path(base, p) or p
                for p in _parse_string_list(ignore_file_raw, field="ignore_path_file")
            ),
        )

    ignore_mod_raw = _first_ci(spec, "ignore_module", "ignore-module")
    if ignore_mod_raw is not None:
        out = replace(
            out,
            ignore_module=tuple(
                _parse_string_list(ignore_mod_raw, field="ignore_module")
            ),
        )

    ignore_fl_raw = _first_ci(spec, "ignore_filelist", "ignore-filelist")
    if ignore_fl_raw is not None:
        out = replace(
            out,
            ignore_filelist=tuple(
                _parse_string_list(ignore_fl_raw, field="ignore_filelist")
            ),
        )

    jobs_raw = _first_ci(spec, "jobs", "j", "job", "workers")
    if jobs_raw is not None:
        out = replace(out, jobs=_parse_jobs(jobs_raw))

    if _mapping_get_ci(spec, "low_memory") is not None:
        out = replace(out, low_memory=bool(_mapping_get_ci(spec, "low_memory")))

    cache_dir = _first_ci(spec, "cache_dir")
    if cache_dir:
        out = replace(out, cache_dir=_resolve_path(base, str(cache_dir)))

    if _mapping_get_ci(spec, "no_cache") is not None:
        out = replace(out, no_cache=bool(_mapping_get_ci(spec, "no_cache")))

    if _mapping_get_ci(spec, "refresh_cache") is not None:
        out = replace(out, refresh_cache=bool(_mapping_get_ci(spec, "refresh_cache")))

    if _mapping_get_ci(spec, "max_depth") is not None:
        out = replace(out, max_depth=int(_mapping_get_ci(spec, "max_depth")))

    index_cwd = _first_ci(spec, "index_cwd", "index-cwd")
    if index_cwd:
        out = replace(out, index_cwd=_resolve_path(base, str(index_cwd)))

    if _mapping_get_ci(spec, "quiet") is not None:
        out = replace(out, quiet=bool(_mapping_get_ci(spec, "quiet")))

    log_file = _mapping_get_ci(spec, "log_file")
    if log_file:
        out = replace(out, log_file=_resolve_path(base, str(log_file)))

    if _mapping_get_ci(spec, "no_log_file") is not None:
        out = replace(out, no_log_file=bool(_mapping_get_ci(spec, "no_log_file")))

    return out


def _bool_field(spec: Mapping[str, Any], *keys: str, default: bool = False) -> bool:
    for key in keys:
        hit = _mapping_get_ci(spec, key)
        if hit is not None:
            return bool(hit)
    return default


def _optional_bool(spec: Mapping[str, Any], key: str) -> Optional[bool]:
    hit = _mapping_get_ci(spec, key)
    if hit is None:
        return None
    if not isinstance(hit, bool):
        raise ValueError(f"{key!r} must be boolean or null")
    return hit


def run_config_for_full_index(
    shared: RunConfig,
    entry: RunTestEntry,
    spec: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
) -> RunConfig:
    from pathlib import Path

    base = base_dir or Path.cwd()
    cfg = _merge_full_index_fields(shared, spec, base_dir=base)

    out_raw = _mapping_get_ci(spec, "output")
    output = (
        _resolve_path(base, str(out_raw).strip())
        if out_raw is not None and str(out_raw).strip()
        else cfg.output
    )

    from hierwalk.search_spec import resolve_search_spec

    mode = entry.mode
    find_top = mode == "find-top"
    raw_search = _mapping_get_ci(spec, "search")
    search_spec = resolve_search_spec(spec)
    search = None
    search_path = None
    search_case_insensitive = False
    if search_spec is not None:
        search_case_insensitive = search_spec.case_insensitive
    if not isinstance(raw_search, Mapping):
        search = str(raw_search or "").strip() or None
        search_path = (
            str(_mapping_get_ci(spec, "search_path") or "").strip() or None
        )

    return replace(
        cfg,
        mode=mode,
        output=output or "-",
        find_top=find_top,
        search=search,
        search_path=search_path,
        search_subtree=bool(_mapping_get_ci(spec, "search_subtree") or False),
        search_module=bool(_mapping_get_ci(spec, "search_module") or False),
        search_case_insensitive=search_case_insensitive,
        search_spec=search_spec,
        check_connect=None,
        check_connect_batch=None,
        connect_inline=None,
        inst_trace=None,
        fanin_cone=None,
        fanout_cone=None,
        flat_suite_step=True,
        full_index_step=True,
    )


def run_config_for_test(
    shared: RunConfig,
    entry: RunTestEntry,
    spec: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
    full_index_spec: Optional[Mapping[str, Any]] = None,
    full_index_enabled: bool = False,
) -> RunConfig:
    """Merge shared + run_on_full_index + one verification block into RunConfig."""
    from pathlib import Path

    base = base_dir or Path.cwd()
    cfg = shared
    if full_index_spec is not None and full_index_enabled:
        cfg = _merge_full_index_fields(cfg, full_index_spec, base_dir=base)

    index_strategy = resolve_verification_index_strategy(
        entry.mode,
        full_index_spec=full_index_spec,
        full_index_enabled=full_index_enabled,
    )

    out_raw = _mapping_get_ci(spec, "output")
    output = (
        _resolve_path(base, str(out_raw).strip())
        if out_raw is not None and str(out_raw).strip()
        else cfg.output
    )

    include_ff = _bool_field(spec, "include_ff", "include-ff", default=cfg.include_ff)
    ff_barrier = _first_ci(spec, "ff_barrier", "ff-barrier")
    if ff_barrier is not None:
        include_ff = not bool(ff_barrier)

    connect_trace = _bool_field(
        spec,
        "connect_trace",
        "connect-trace",
        "trace",
        default=cfg.connect_trace,
    )
    connect_log = _bool_field(spec, "connect_log", "connect-log", default=cfg.connect_log)
    over_approx = _optional_bool(spec, "over_approximate_if")
    if over_approx is None:
        over_approx = cfg.over_approximate_if
    strict_generate = _bool_field(spec, "strict_generate", default=cfg.strict_generate)

    if entry.kind == RUN_CONN_CHECK:
        check_connect = None
        connect_inline: Optional[Any] = None
        check_connect_batch: Optional[str] = None

        checks = _mapping_get_ci(spec, "checks")
        check_connect_raw = _first_ci(spec, "check_connect", "check-connect")

        exec_mode = exec_mode_for_verification(entry.kind, entry.mode)
        if exec_mode == "check-connect":
            if check_connect_raw is not None:
                check_connect = _parse_check_connect(check_connect_raw)
            elif isinstance(checks, list) and len(checks) == 1:
                check_connect = _parse_check_connect(checks[0])
            else:
                raise ValueError(
                    f"{entry.kind} needs check_connect or a single checks[] item"
                )
        else:
            if checks is not None:
                connect_inline = dict(spec)
            elif check_connect_raw is not None:
                parsed = _parse_check_connect(check_connect_raw)
                connect_inline = {"checks": [{"a": parsed[0], "b": parsed[1]}]}
            else:
                raise ValueError(f"{entry.kind} requires checks")

        return replace(
            cfg,
            mode=exec_mode,
            index_strategy=index_strategy,
            output=output or "-",
            check_connect=check_connect,
            connect_inline=connect_inline,
            check_connect_batch=check_connect_batch,
            inst_trace=None,
            fanin_cone=None,
            fanout_cone=None,
            connect_trace=connect_trace,
            connect_log=connect_log,
            include_ff=include_ff,
            strict_generate=strict_generate,
            over_approximate_if=over_approx,
            flat_suite_step=True,
            verification_step_kind=entry.kind,
            verification_step_name=entry.name or f"{entry.kind}[{entry.index}]",
        )

    if entry.kind == RUN_IO_TRACE:
        inst_req: InstTraceRequest = parse_inst_trace_json(
            spec,
            top=cfg.top or "",
            defines=cfg.defines_map,
        )
        exec_mode = exec_mode_for_verification(entry.kind, entry.mode)
        return replace(
            cfg,
            mode=exec_mode,
            index_strategy=index_strategy,
            output=output or "-",
            inst_trace=inst_req,
            check_connect=None,
            check_connect_batch=None,
            connect_inline=None,
            fanin_cone=None,
            fanout_cone=None,
            over_approximate_if=over_approx,
            flat_suite_step=True,
            verification_step_kind=entry.kind,
            verification_step_name=entry.name or f"{entry.kind}[{entry.index}]",
        )

    fanin = str(_first_ci(spec, "fanin_cone", "fanin-cone", "endpoint") or "").strip() or None
    fanout = str(_first_ci(spec, "fanout_cone", "fanout-cone") or "").strip() or None
    exec_mode = exec_mode_for_verification(entry.kind, entry.mode)
    cone_graph = _resolve_path(base, _first_ci(spec, "cone_graph", "cone-graph"))

    return replace(
        cfg,
        mode=exec_mode,
        index_strategy=index_strategy,
        output=output or "-",
        fanin_cone=fanin,
        fanout_cone=fanout,
        cone_graph=cone_graph,
        inst_trace=None,
        check_connect=None,
        check_connect_batch=None,
        connect_inline=None,
        over_approximate_if=over_approx,
        flat_suite_step=True,
        verification_step_kind=entry.kind,
        verification_step_name=entry.name or f"{entry.kind}[{entry.index}]",
    )


def parse_flat_run_suite(
    data: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
    raw_text: Optional[str] = None,
) -> RunTestSuite:
    """
    Parse flat run JSON with sibling blocks at the top level.

    Example::

        {
          "filelist": "design.f",
          "top": "top",
          "run_on_full_index": {
            "enable": 1,
            "mode": "hierarchy",
            "ignore_path": ["pcielinktop"],
            "jobs": 4,
            "output": "instances.tsv"
          },
          "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "checks": [{"id": "a", "a": "top.a", "b": "top.b"}]
          },
          "run_io_trace": {"enable": 0, "mode": "full-index", "instance": "top.u0"},
          "run_cone_trace": {
            "enable": 1,
            "mode": "full-index",
            "fanout_cone": "top.u0.din"
          }
        }
    """
    present: list[str] = []
    for k in _FLAT_BLOCK_ORDER:
        if k == RUN_ON_FULL_INDEX:
            if _full_index_block_key(data) is not None:
                present.append(k)
        elif _mapping_get_ci(data, k) is not None:
            present.append(k)
    if not present:
        raise ValueError(
            "flat run JSON needs at least one of "
            + ", ".join(TEST_KINDS)
        )

    shared = parse_shared_run_request_json(data, base_dir=base_dir)
    shared = replace(
        shared,
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
    )

    full_index_spec, full_index_enabled, full_index_warnings = _full_index_enable_state(
        data,
        raw_text=raw_text,
    )
    full_index_key = _full_index_block_key(data)
    enable_warnings: list[str] = list(full_index_warnings)

    entries: list[RunTestEntry] = []
    index = 0
    for kind in _FLAT_BLOCK_ORDER:
        if kind == RUN_ON_FULL_INDEX:
            if full_index_key is None:
                continue
            spec_raw = _mapping_get_ci(data, full_index_key)
            block_label = full_index_key
        else:
            spec_raw = _mapping_get_ci(data, kind)
            block_label = kind
        if spec_raw is None:
            continue
        if not isinstance(spec_raw, Mapping):
            raise ValueError(f"{kind!r} must be an object")
        if kind == RUN_ON_FULL_INDEX:
            enabled = full_index_enabled
        else:
            enabled, step_warnings = resolve_block_enabled(
                spec_raw,
                default=True,
                document=data,
                block_key=block_label,
                raw_text=raw_text,
            )
            enable_warnings.extend(step_warnings)
        if not enabled:
            continue
        label = kind
        entry = _parse_test_entry_from_spec(
            kind,
            spec_raw,
            label=label,
            index=index,
            enabled=True,
        )
        entries.append(entry)
        index += 1

    if not entries:
        raise ValueError("no enabled steps (all blocks have enable: 0)")

    return RunTestSuite(
        shared=shared,
        tests=tuple(entries),
        full_index_spec=full_index_spec,
        full_index_enabled=full_index_enabled,
        enable_warnings=tuple(enable_warnings),
    )


def _kinds_in_item(item: Mapping[str, Any]) -> list[str]:
    return [k for k in VERIFICATION_KINDS if _mapping_get_ci(item, k) is not None]


_ENABLE_TYPO_HINTS = (
    "run_on_full_inex",
    "run_on_full_indx",
    "run_on_ful_index",
)


def detect_enable_key_typos(data: Mapping[str, Any]) -> Tuple[str, ...]:
    """Warn on block keys that look like run_on_full_index typos (not legacy db)."""
    hints: list[str] = []
    known = frozenset(
        {
            RUN_ON_FULL_INDEX,
            RUN_ON_FULL_DB_LEGACY,
            RUN_CONN_CHECK,
            RUN_IO_TRACE,
            RUN_CONE_TRACE,
            "filelist",
            "top",
            "defines",
            "tests",
            "mode",
            "output",
            "env",
            "environment",
        }
    )
    for raw_key in data:
        if not isinstance(raw_key, str):
            continue
        low = raw_key.lower()
        if low in known or low.startswith("//"):
            continue
        if "full" in low and "index" in low.replace("_", "") and low not in known:
            hints.append(raw_key)
        elif low in _ENABLE_TYPO_HINTS:
            hints.append(raw_key)
    return tuple(hints)


def format_suite_enable_trace(
    document: Mapping[str, Any],
    suite: RunTestSuite,
    plan: Sequence[Tuple[RunTestEntry, RunConfig]],
    *,
    raw_text: Optional[str] = None,
) -> Tuple[str, ...]:
    """
    Human-readable enable path from JSON blocks → suite.tests → RunConfig → execute_run.
    """
    lines: list[str] = []
    lines.append(
        "enable-trace: stage=parse_flat_suite "
        f"full_index_block_enabled={suite.full_index_enabled} "
        f"scheduled_steps={len(suite.tests)}"
    )

    full_index_key = _full_index_block_key(document)
    for kind in TEST_KINDS:
        if kind == RUN_ON_FULL_INDEX:
            if full_index_key is None:
                continue
            block_key = full_index_key
            spec_raw = _mapping_get_ci(document, full_index_key)
        else:
            block_key = kind
            spec_raw = _mapping_get_ci(document, kind)
        if spec_raw is None:
            continue
        if not isinstance(spec_raw, Mapping):
            lines.append(
                f"enable-trace: block={block_key} raw_enable=? parsed=? "
                "action=ERROR (not an object)"
            )
            continue
        raw_enable = block_enable_raw(spec_raw)
        parsed, _ = resolve_block_enabled(
            spec_raw,
            default=True,
            document=document,
            block_key=block_key,
            raw_text=raw_text,
        )
        if raw_enable is None and parsed:
            default_note = "enable/enabled missing, default=1"
        else:
            default_note = ""
        if not parsed:
            action = "SKIP"
            detail = "not added to suite.tests; settings not merged"
        elif kind == RUN_ON_FULL_INDEX:
            action = "SCHEDULE"
            detail = f"mode={_mapping_get_ci(spec_raw, 'mode') or 'hierarchy'}"
        else:
            action = "SCHEDULE"
            raw_mode = _mapping_get_ci(spec_raw, "mode")
            detail = f"block_mode={raw_mode or _DEFAULT_INDEX_MODE.get(kind, '?')}"
        lines.append(
            f"enable-trace: block={block_key} raw_enable={raw_enable!r} "
            f"parsed_enable={int(parsed)} action={action} {detail}"
            + (f" ({default_note})" if default_note else "")
        )

    for i, (entry, cfg) in enumerate(plan):
        merge = (
            entry.kind != RUN_ON_FULL_INDEX
            and suite.full_index_spec is not None
            and suite.full_index_enabled
        )
        index_path = (
            "path-walk"
            if (
                cfg.index_strategy == "path-walk"
                or cfg.mode == "path-walk"
            )
            else "full-index(load_or_build_index)"
        )
        if entry.kind == RUN_ON_FULL_INDEX:
            index_path = f"hierarchy-step(mode={cfg.mode})"
        lines.append(
            f"enable-trace: stage=build_config step={i} kind={entry.kind} "
            f"cfg.mode={cfg.mode} cfg.index_strategy={cfg.index_strategy} "
            f"merge_full_index_settings={int(merge)}"
        )
        lines.append(
            f"enable-trace: stage=pre_execute step={i} kind={entry.kind} "
            f"index_path={index_path}"
        )
    return tuple(lines)


def list_disabled_suite_blocks(
    data: Mapping[str, Any],
    *,
    raw_text: Optional[str] = None,
) -> Tuple[str, ...]:
    """Return suite block names present in JSON but skipped via ``enable: 0``."""
    disabled: list[str] = []
    if "tests" in data:
        tests_raw = data.get("tests")
        if isinstance(tests_raw, list):
            for i, item in enumerate(tests_raw):
                if not isinstance(item, Mapping):
                    continue
                kinds = _kinds_in_item(item)
                if len(kinds) != 1:
                    continue
                kind = kinds[0]
                spec = _spec_block(item, kind)
                if not block_enabled(spec, default=True):
                    label = str(
                        _mapping_get_ci(item, "name")
                        or _mapping_get_ci(item, "id")
                        or f"tests[{i}]"
                    ).strip()
                    disabled.append(label or kind)
        return tuple(disabled)

    for kind in TEST_KINDS:
        if kind == RUN_ON_FULL_INDEX:
            key = _full_index_block_key(data)
            if key is None:
                continue
            spec_raw = _mapping_get_ci(data, key)
            block_label = key
        else:
            spec_raw = _mapping_get_ci(data, kind)
            block_label = kind
        if spec_raw is None:
            continue
        if not isinstance(spec_raw, Mapping):
            continue
        enabled, _ = resolve_block_enabled(
            spec_raw,
            default=True,
            document=data,
            block_key=block_label,
            raw_text=raw_text,
        )
        if not enabled:
            disabled.append(block_label)
    return tuple(disabled)


def parse_legacy_tests_array_suite(
    data: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
    raw_text: Optional[str] = None,
) -> RunTestSuite:
    """Legacy ``tests`` array format (backward compatible)."""
    tests_raw = data.get("tests")
    if tests_raw is None:
        raise ValueError("tests array missing")
    if not isinstance(tests_raw, list) or not tests_raw:
        raise ValueError("'tests' must be a non-empty JSON array")

    shared = parse_shared_run_request_json(data, base_dir=base_dir)
    shared = replace(
        shared,
        mode=None,
        check_connect=None,
        check_connect_batch=None,
        connect_inline=None,
        inst_trace=None,
        fanin_cone=None,
        fanout_cone=None,
    )

    entries: list[RunTestEntry] = []
    for i, item in enumerate(tests_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"tests[{i}] must be an object")
        kinds = _kinds_in_item(item)
        if len(kinds) != 1:
            raise ValueError(
                f"tests[{i}] needs exactly one of {', '.join(VERIFICATION_KINDS)}"
            )
        kind = kinds[0]
        spec = _spec_block(item, kind)
        enabled = block_enabled(spec, default=True)
        if not enabled:
            continue
        name = str(_mapping_get_ci(item, "name") or _mapping_get_ci(item, "id") or "").strip()
        entries.append(
            _parse_test_entry_from_spec(
                kind,
                spec,
                label=f"tests[{i}]",
                index=len(entries),
                enabled=True,
                name=name,
            )
        )

    if not entries:
        raise ValueError("no enabled steps in tests array")

    full_index_spec, full_index_enabled, full_index_warnings = _full_index_enable_state(
        data,
        raw_text=raw_text,
    )

    return RunTestSuite(
        shared=shared,
        tests=tuple(entries),
        full_index_spec=full_index_spec,
        full_index_enabled=full_index_enabled,
        enable_warnings=full_index_warnings,
    )


def parse_run_test_suite(
    data: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
    raw_text: Optional[str] = None,
) -> RunTestSuite:
    if "tests" in data:
        return parse_legacy_tests_array_suite(
            data,
            base_dir=base_dir,
            raw_text=raw_text,
        )
    return parse_flat_run_suite(data, base_dir=base_dir, raw_text=raw_text)


def try_parse_run_test_suite(
    data: Any,
    *,
    base_dir: Optional[Any] = None,
    raw_text: Optional[str] = None,
) -> Optional[RunTestSuite]:
    if not isinstance(data, Mapping):
        return None
    has_flat = any(_mapping_get_ci(data, k) is not None for k in TEST_KINDS) or (
        _full_index_block_key(data) is not None
    )
    has_legacy = "tests" in data
    if not has_flat and not has_legacy:
        return None
    return parse_run_test_suite(data, base_dir=base_dir, raw_text=raw_text)


def spec_for_test_entry(
    document: Mapping[str, Any],
    entry: RunTestEntry,
) -> Mapping[str, Any]:
    if "tests" in document:
        tests_raw = document.get("tests")
        if not isinstance(tests_raw, list):
            raise ValueError("tests must be an array")
        # Legacy: map entry.index to enabled-only list — use kind scan
        seen = 0
        for item in tests_raw:
            if not isinstance(item, Mapping):
                continue
            kinds = _kinds_in_item(item)
            if len(kinds) != 1:
                continue
            kind = kinds[0]
            spec = _spec_block(item, kind)
            if not block_enabled(spec, default=True):
                continue
            if seen == entry.index:
                return spec
            seen += 1
        raise ValueError(f"tests[{entry.index}] missing from document")

    if entry.kind == RUN_ON_FULL_INDEX:
        key = _full_index_block_key(document)
        if key is None:
            raise ValueError(f"{RUN_ON_FULL_INDEX} block missing")
        return _spec_block(document, key)
    return _spec_block(document, entry.kind)


def build_test_run_configs(
    suite: RunTestSuite,
    document: Mapping[str, Any],
    *,
    base_dir: Optional[Any] = None,
) -> Sequence[Tuple[RunTestEntry, RunConfig]]:
    out: list[Tuple[RunTestEntry, RunConfig]] = []
    for entry in suite.tests:
        spec = spec_for_test_entry(document, entry)
        if entry.kind == RUN_ON_FULL_INDEX:
            cfg = run_config_for_full_index(
                suite.shared,
                entry,
                spec,
                base_dir=base_dir,
            )
        else:
            cfg = run_config_for_test(
                suite.shared,
                entry,
                spec,
                base_dir=base_dir,
                full_index_spec=suite.full_index_spec,
                full_index_enabled=suite.full_index_enabled,
            )
        out.append((entry, cfg))
    return tuple(out)