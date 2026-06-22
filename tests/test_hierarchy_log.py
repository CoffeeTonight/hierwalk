"""Hierarchy provenance in logs and error messages."""

from __future__ import annotations

from hierwalk.connect_endpoints import _explain_hierarchy_miss
from hierwalk.hierarchy_log import (
    format_hierarchy_row_line,
    format_row_provenance,
    hierarchy_spine_between,
    scopes_from_hop_detail,
)
from hierwalk.models import FlatRow


def _row(path: str, *, file: str = "/rtl/top.v", via: str = "/lists/design.f") -> FlatRow:
    parts = path.split(".")
    return FlatRow(
        full_path=path,
        inst_leaf=parts[-1],
        module=parts[-1],
        depth=len(parts) - 1,
        parent_path=".".join(parts[:-1]) if len(parts) > 1 else None,
        file=file,
        via_filelist=via,
        filelist_chain=f"{via} > {via}",
    )


def test_format_row_provenance_includes_file_and_filelist():
    row = _row("top.u_child", file="/proj/child.v", via="/proj/filelist.f")
    text = format_row_provenance(row)
    assert "rtl= /proj/child.v" in text
    assert "via_filelist=/proj/filelist.f" in text
    assert "filelist_chain=" in text


def test_explain_hierarchy_miss_lists_children_with_sources():
    rows = {
        "top": _row("top", file="/rtl/top.v"),
        "top.u_ok": _row("top.u_ok", file="/rtl/ok.v"),
    }
    # Fake index — only used when remainder has port-like suffix
    class _Idx:
        def get_module(self, _name):
            return None

    errors = _explain_hierarchy_miss(
        "top.u_missing.port",
        rows,
        index=_Idx(),  # type: ignore[arg-type]
        top="top",
        broken_prefix="top.u_missing",
    )
    joined = "\n".join(errors)
    assert "path stops at 'top'" in joined
    assert "rtl= /rtl/top.v" in joined
    assert "top.u_ok" in joined
    assert "via_filelist=" in joined


def test_format_hierarchy_row_line():
    row = _row("top.u0")
    line = format_hierarchy_row_line(row)
    assert line.startswith("top.u0")
    assert "module=u0" in line


def test_hierarchy_spine_between_endpoints():
    spine = hierarchy_spine_between("top", "top.u_spine.u_leaf")
    assert spine == ["top", "top.u_spine", "top.u_spine.u_leaf"]


def test_scopes_from_hop_detail():
    detail = (
        "top:probe_in -> top.u_spine:net0 "
        "(instance u_spine port .net0 in top)"
    )
    assert scopes_from_hop_detail(detail) == ["top", "top.u_spine"]
    assert scopes_from_hop_detail("structural COI path") == []


def test_emit_connect_log_includes_success_endpoint_provenance():
    from hierwalk.connectivity import emit_connect_trace_log
    from hierwalk.hierarchy_log import format_endpoint_provenance_line
    from hierwalk.models import ConnectEndpoint, ConnectResult

    rows = {
        "top.a": _row("top.a", file="/rtl/a.v"),
        "top.z": _row("top.z", file="/rtl/z.v"),
    }
    ep_a = ConnectEndpoint("top.a", "top.a", "p0", "top", port_found=True)
    ep_b = ConnectEndpoint("top.z", "top.z", "p1", "top", port_found=True)
    result = ConnectResult(ep_a, ep_b, True, "port-port", check_id="ok")
    line_a = format_endpoint_provenance_line("A", ep_a, rows)
    assert "rtl= /rtl/a.v" in line_a
    assert "via_filelist=" in line_a
    import io

    buf = io.StringIO()
    emit_connect_trace_log(result, stream=buf, check_prefix="ok", rows_by_path=rows)
    text = buf.getvalue()
    assert "[ok]" in text
    assert "rtl= /rtl/a.v" in text
    assert "rtl= /rtl/z.v" in text
    assert "connected: True" in text
    assert "path hierarchy (rtl + filelist):" in text