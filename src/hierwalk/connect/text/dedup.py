"""Text-conn batch dedup keys (coarse base-level)."""

from __future__ import annotations

from typing import Any, Sequence, Tuple

from hierwalk.connect.shared.modes import _has_port, _mode
from hierwalk.inst_scan import coarse_hierarchy_path
from hierwalk.models import ConnectEndpoint, ConnectResult


def coarse_inst_path(inst_path: str) -> str:
    return coarse_hierarchy_path(inst_path)


def coarse_port_base(port_name: str) -> str:
    if not port_name:
        return ""
    return port_name.split("[", 1)[0].split(".", 1)[0]


def text_dedup_key(
    ep_a: ConnectEndpoint,
    ep_b: ConnectEndpoint,
    errors: Sequence[str],
) -> Tuple[Any, ...]:
    """Coarse text-conn grep key: strip slice/index from inst paths and port names."""
    mode = _mode(ep_a, ep_b) if ep_a.module and ep_b.module else "unknown"
    if errors:
        return (
            "err",
            mode,
            coarse_inst_path(ep_a.inst_path),
            coarse_port_base(ep_a.port_name or ""),
            coarse_inst_path(ep_b.inst_path),
            coarse_port_base(ep_b.port_name or ""),
            tuple(errors),
        )
    if (_has_port(ep_a) and not ep_a.port_found) or (
        _has_port(ep_b) and not ep_b.port_found
    ):
        return (
            "port-miss",
            mode,
            coarse_inst_path(ep_a.inst_path),
            coarse_port_base(ep_a.port_name or ""),
            bool(ep_a.port_found),
            coarse_inst_path(ep_b.inst_path),
            coarse_port_base(ep_b.port_name or ""),
            bool(ep_b.port_found),
        )
    return (
        "grep",
        mode,
        coarse_inst_path(ep_a.inst_path),
        coarse_port_base(ep_a.port_name or ""),
        coarse_inst_path(ep_b.inst_path),
        coarse_port_base(ep_b.port_name or ""),
    )


_text_coi_dedup_key = text_dedup_key


def fanout_text_result(
    template: ConnectResult,
    *,
    spec_a: str,
    spec_b: str,
    ep_a: ConnectEndpoint,
    ep_b: ConnectEndpoint,
    check_id: str,
) -> ConnectResult:
    """Copy a deduped text-grep verdict onto a leaf check (original specs/endpoints)."""
    return ConnectResult(
        ConnectEndpoint(
            spec_a,
            ep_a.inst_path,
            ep_a.port_name,
            ep_a.module,
            ep_a.port_found,
        ),
        ConnectEndpoint(
            spec_b,
            ep_b.inst_path,
            ep_b.port_name,
            ep_b.module,
            ep_b.port_found,
        ),
        template.connected,
        template.mode,
        hops=list(template.hops),
        errors=list(template.errors),
        note=template.note,
        check_id=check_id,
        walk_notes=list(template.walk_notes),
        coi_walk=template.coi_walk,
    )


_fanout_text_coi_result = fanout_text_result