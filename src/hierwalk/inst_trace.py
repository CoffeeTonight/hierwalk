"""Instance-level driver/sinker trace from a single hierarchy path."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, IO, List, Mapping, Optional, Sequence, Set, Tuple

from hierwalk.cone import ConeBoundary, ConeResult, fanin_cone, fanout_cone
from hierwalk.connect.shared.endpoints import resolve_endpoint
from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow, PortInfo
from hierwalk.params import resolve_param_map
from hierwalk.path_refine import refine_param_ctx_for_path
from hierwalk.port_scan import port_index_for_design_module, scan_ports_detail_from_module_text
from hierwalk.trace_stop import TraceStopPolicy, parse_trace_stop_policy


_DIRECTION_ALIASES: Dict[str, str] = {
    "driver": "driver",
    "drivers": "driver",
    "in": "driver",
    "driver-in": "driver",
    "driver_in": "driver",
    "fanin": "driver",
    "sinker": "sinker",
    "sinkers": "sinker",
    "out": "sinker",
    "sinker-out": "sinker",
    "sinker_out": "sinker",
    "fanout": "sinker",
    "both": "both",
    "all": "both",
}

_PATH_KIND_ALIASES: Dict[str, str] = {
    "ff": "ff",
    "seq": "ff",
    "sequential": "ff",
    "comb": "comb",
    "combo": "comb",
    "combinational": "comb",
    "combination": "comb",
}


@dataclass(frozen=True)
class InstTraceRequest:
    instance: str
    direction: str = "both"
    path_kind: str = "ff"
    top: str = ""
    defines: Mapping[str, str] = field(default_factory=dict)
    over_approximate_if: Optional[bool] = None
    ignore_hierarchy: Tuple[str, ...] = ()
    trace_max_depth: Optional[int] = None

    @property
    def trace_stop(self) -> TraceStopPolicy:
        return TraceStopPolicy(
            ignore_hierarchy=self.ignore_hierarchy,
            trace_max_depth=self.trace_max_depth,
        )


@dataclass
class InstTracePortResult:
    port_name: str
    port_direction: str
    trace_direction: str
    cone: ConeResult


@dataclass
class InstTraceResult:
    instance: str
    module: str
    direction: str
    path_kind: str
    port_results: List[InstTracePortResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def boundaries(self) -> List[Tuple[InstTracePortResult, ConeBoundary]]:
        out: List[Tuple[InstTracePortResult, ConeBoundary]] = []
        for pr in self.port_results:
            for b in pr.cone.boundaries:
                out.append((pr, b))
        return out


def normalize_trace_direction(raw: str) -> str:
    key = str(raw or "").strip().lower().replace("_", "-")
    if not key:
        return "both"
    out = _DIRECTION_ALIASES.get(key)
    if out is None:
        allowed = sorted(set(_DIRECTION_ALIASES.values()) | set(_DIRECTION_ALIASES))
        raise ValueError(
            f"unknown inst_trace direction {raw!r}; expected one of {allowed}"
        )
    return out


def normalize_path_kind(raw: str) -> str:
    key = str(raw or "").strip().lower().replace("_", "-")
    if not key:
        return "ff"
    out = _PATH_KIND_ALIASES.get(key)
    if out is None:
        allowed = sorted(set(_PATH_KIND_ALIASES.values()) | set(_PATH_KIND_ALIASES))
        raise ValueError(
            f"unknown inst_trace path_kind {raw!r}; expected one of {allowed}"
        )
    return out


def parse_inst_trace_json(
    data: object,
    *,
    top: str = "",
    defines: Optional[Mapping[str, str]] = None,
) -> InstTraceRequest:
    base_defines = dict(defines or {})
    if isinstance(data, str):
        instance = data.strip()
        if not instance:
            raise ValueError("inst_trace instance must be non-empty")
        return InstTraceRequest(
            instance=instance,
            top=top,
            defines=base_defines,
        )
    if not isinstance(data, Mapping):
        raise ValueError("inst_trace must be a string or object")

    instance = str(
        data.get("instance")
        or data.get("inst")
        or data.get("path")
        or ""
    ).strip()
    if not instance:
        raise ValueError("inst_trace requires 'instance' (hierarchy path)")

    direction = normalize_trace_direction(
        str(data.get("direction") or data.get("dir") or "both")
    )
    path_raw = (
        data.get("path_kind")
        or data.get("path-kind")
        or data.get("path_type")
        or data.get("path-type")
        or data.get("ff_comb")
        or data.get("ff-comb")
        or data.get("ff/comb")
        or "ff"
    )
    path_kind = normalize_path_kind(str(path_raw))

    req_top = str(data.get("top") or top or "").strip()
    req_defines = dict(base_defines)
    extra = data.get("defines") or {}
    if extra:
        if not isinstance(extra, Mapping):
            raise ValueError("inst_trace.defines must be an object")
        req_defines.update({str(k): str(v) for k, v in extra.items()})

    over_approx = data.get("over_approximate_if")
    if over_approx is not None and not isinstance(over_approx, bool):
        raise ValueError("inst_trace.over_approximate_if must be boolean or null")

    stop = parse_trace_stop_policy(data)

    return InstTraceRequest(
        instance=instance,
        direction=direction,
        path_kind=path_kind,
        top=req_top,
        defines=req_defines,
        over_approximate_if=over_approx,
        ignore_hierarchy=stop.ignore_hierarchy,
        trace_max_depth=stop.trace_max_depth,
    )


def _param_ctx_for_row(
    index: DesignIndex,
    row: FlatRow,
    top: str,
) -> Mapping[str, str]:
    if top:
        refined = refine_param_ctx_for_path(index, top, row.full_path)
        if refined.ok and refined.param_ctx:
            return refined.param_ctx
    if row.param_ctx:
        return row.param_ctx
    rec = index.get_module(row.module)
    if not rec:
        return {}
    return resolve_param_map(rec.raw_params)


def _port_decl_direction(info: PortInfo) -> str:
    decl = info.decl.lower()
    m = re.search(r"\b(input|output|inout)\b", decl)
    if m:
        return m.group(1).lower()
    if decl.startswith("input"):
        return "input"
    if decl.startswith("output"):
        return "output"
    if decl.startswith("inout"):
        return "inout"
    return "unknown"


def _ports_for_instance(
    index: DesignIndex,
    row: FlatRow,
    top: str,
) -> List[Tuple[str, str]]:
    ctx = _param_ctx_for_row(index, row, top)
    port_index = port_index_for_design_module(index, row.module, ctx)
    if port_index:
        out: List[Tuple[str, str]] = []
        seen: Set[str] = set()
        for info in port_index.values():
            direction = _port_decl_direction(info)
            for name in info.names:
                if name in seen:
                    continue
                seen.add(name)
                out.append((name, direction))
        return sorted(out, key=lambda x: x[0])
    rec = index.get_module(row.module)
    if not rec or not rec.file_path:
        return []
    text = index._source_text(rec.file_path)
    if not text:
        return []
    out = []
    seen: Set[str] = set()
    for info in scan_ports_detail_from_module_text(
        text,
        row.module,
        param_ctx=ctx,
    ):
        direction = _port_decl_direction(info)
        for name in info.names:
            if name in seen:
                continue
            seen.add(name)
            out.append((name, direction))
    return sorted(out, key=lambda x: x[0])


def _seed_ports(
    ports: Sequence[Tuple[str, str]],
    direction: str,
) -> List[Tuple[str, str, str]]:
    """Return (port_name, port_direction, trace_direction) seeds."""
    seeds: List[Tuple[str, str, str]] = []
    for name, port_dir in ports:
        if direction in ("driver", "both") and port_dir in ("input", "inout"):
            seeds.append((name, port_dir, "driver"))
        if direction in ("sinker", "both") and port_dir in ("output", "inout"):
            seeds.append((name, port_dir, "sinker"))
    return seeds


def run_inst_trace(
    request: InstTraceRequest,
    *,
    rows: Sequence[FlatRow],
    index: DesignIndex,
    top: str,
    defines: Optional[Mapping[str, str]] = None,
) -> InstTraceResult:
    top_name = (request.top or top or "").strip()
    compile_defines = dict(index.effective_defines())
    if defines:
        compile_defines.update(defines)
    compile_defines.update(request.defines)
    over_approx = (
        request.over_approximate_if
        if request.over_approximate_if is not None
        else True
    )

    ep, ep_errs = resolve_endpoint(
        request.instance,
        rows,
        index,
        top=top_name,
        require_port=False,
    )
    result = InstTraceResult(
        instance=request.instance,
        module=ep.module,
        direction=request.direction,
        path_kind=request.path_kind,
        errors=list(ep_errs),
    )
    if ep_errs:
        return result

    row = next((r for r in rows if r.full_path == ep.inst_path), None)
    if row is None:
        result.errors.append(f"hierarchy not found: {ep.inst_path}")
        return result

    ports = _ports_for_instance(index, row, top_name)
    if not ports:
        result.errors.append(
            f"no ports parsed for instance {ep.inst_path} (module {row.module})"
        )
        return result

    seeds = _seed_ports(ports, request.direction)
    if not seeds:
        result.errors.append(
            f"no ports match direction={request.direction!r} on {ep.inst_path}"
        )
        return result

    for port_name, port_dir, trace_dir in seeds:
        endpoint = f"{ep.inst_path}.{port_name}"
        if trace_dir == "driver":
            cone = fanin_cone(
                endpoint,
                rows=rows,
                index=index,
                top=top_name,
                defines=compile_defines,
                over_approximate_if=over_approx,
                path_kind=request.path_kind,
                trace_stop=request.trace_stop,
            )
        else:
            cone = fanout_cone(
                endpoint,
                rows=rows,
                index=index,
                top=top_name,
                defines=compile_defines,
                over_approximate_if=over_approx,
                path_kind=request.path_kind,
                trace_stop=request.trace_stop,
            )
        if cone.errors:
            result.errors.extend(
                f"{endpoint} ({trace_dir}): {err}" for err in cone.errors
            )
        result.port_results.append(
            InstTracePortResult(
                port_name=port_name,
                port_direction=port_dir,
                trace_direction=trace_dir,
                cone=cone,
            )
        )
    return result


def format_inst_trace_tsv(
    result: InstTraceResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> str:
    from hierwalk.hierarchy_log import provenance_fields

    lines = [
        "origin_port\ttrace_direction\tboundary_kind\tscope\tnet\tmodule\tdetail\t"
        "rtl\tvia_filelist\tfilelist_chain",
    ]
    for pr, b in result.boundaries:
        prov = (
            provenance_fields(b.scope, rows_by_path)
            if rows_by_path is not None
            else {}
        )
        lines.append(
            f"{pr.port_name}\t{pr.trace_direction}\t{b.kind}\t{b.scope}\t"
            f"{b.net}\t{b.module}\t{b.detail}\t"
            f"{prov.get('rtl', '')}\t{prov.get('via_filelist', '')}\t"
            f"{prov.get('filelist_chain', '')}"
        )
    lines.append(f"# instance\t{result.instance}")
    lines.append(f"# module\t{result.module}")
    lines.append(f"# direction\t{result.direction}")
    lines.append(f"# path_kind\t{result.path_kind}")
    lines.append(f"# port_traces\t{len(result.port_results)}")
    if rows_by_path is not None:
        inst_prov = provenance_fields(result.instance, rows_by_path)
        lines.append(f"# instance_rtl\t{inst_prov.get('rtl', '')}")
        lines.append(
            f"# instance_via_filelist\t{inst_prov.get('via_filelist', '')}"
        )
        lines.append(
            f"# instance_filelist_chain\t{inst_prov.get('filelist_chain', '')}"
        )
    if result.errors:
        lines.append(f"# errors\t{' | '.join(result.errors)}")
    return "\n".join(lines) + "\n"


def format_inst_trace_report(
    result: InstTraceResult,
    *,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> str:
    from hierwalk.hierarchy_log import format_scope_provenance_line

    lines = [
        f"inst-trace: {result.instance} ({result.module})",
        f"direction={result.direction} path_kind={result.path_kind}",
        f"port traces: {len(result.port_results)}",
    ]
    if result.errors:
        lines.append("errors:")
        lines.extend(f"  - {e}" for e in result.errors)
        return "\n".join(lines) + "\n"
    for pr in result.port_results:
        lines.append(
            f"  [{pr.trace_direction}] port {pr.port_name} ({pr.port_direction}) "
            f"visited={pr.cone.nets_visited} boundaries={len(pr.cone.boundaries)}"
        )
        for b in pr.cone.boundaries:
            lines.append(f"    [{b.kind}] {b.label} ({b.module}) — {b.detail}")
            if rows_by_path is not None and b.scope:
                lines.append(
                    f"      {format_scope_provenance_line(b.scope, rows_by_path)}"
                )
    if not result.port_results:
        lines.append("no port traces (check instance or direction filter)")
    return "\n".join(lines) + "\n"


def print_inst_trace_report(
    result: InstTraceResult,
    *,
    stream: IO[str] = sys.stderr,
    rows_by_path: Optional[Mapping[str, FlatRow]] = None,
) -> None:
    print(
        format_inst_trace_report(result, rows_by_path=rows_by_path),
        end="",
        file=stream,
        flush=True,
    )