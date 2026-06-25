"""Port vs instance leaf name collision (e.g. port ``w_e`` and ``LEAF w_e`` in same module)."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_endpoints import parse_connect_endpoint, resolve_endpoint
from hierwalk.connectivity import check_connectivity
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow
from hierwalk.path_walk import run_path_walk_connect


def _inject_spurious_child_inst(lookup: dict[str, FlatRow], parent_path: str) -> None:
    """Simulate a bad walk that attached an inst leaf matching a parent port name."""
    parent = lookup[parent_path]
    leaf = parent_path.rsplit(".", 1)[-1]
    child_path = f"{parent_path}.w_e"
    lookup[child_path] = FlatRow(
        full_path=child_path,
        inst_leaf="w_e",
        module="LEAF",
        depth=parent.depth + 1,
        parent_path=parent_path,
        file=parent.file,
    )


def _design() -> str:
    return """
    module top;
      D d ();
    endmodule
    module D(output logic w_e);
      LEAF w_e ();
    endmodule
    module LEAF;
      wire stub;
    endmodule
    """


def test_parse_connect_endpoint_prefers_port_over_child_inst_path(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    lookup = {r.full_path: r for r in rows}
    _inject_spurious_child_inst(lookup, "top.d")

    hier, port = parse_connect_endpoint(
        "top.d.w_e",
        lookup,
        index=index,
        top="top",
    )
    assert hier == "top.d"
    assert port == "w_e"


def test_resolve_endpoint_port_not_child_inst_module(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    lookup = {r.full_path: r for r in rows}
    _inject_spurious_child_inst(lookup, "top.d")

    ep, errors = resolve_endpoint(
        "top.d.w_e",
        rows,
        index,
        top="top",
        rows_by_path=lookup,
    )
    assert not errors
    assert ep.inst_path == "top.d"
    assert ep.port_name == "w_e"
    assert ep.port_found
    assert ep.module == "D"


def test_path_walk_does_not_attach_inst_when_tail_is_parent_port(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.d.w_e", "top.d.w_e"),),
        top="top",
    )
    batch, index, state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
    )
    assert "top.d.w_e" not in state.rows_by_path
    ep, errs = resolve_endpoint(
        "top.d.w_e",
        state.rows(),
        index,
        top="top",
        rows_by_path=state.rows_by_path,
    )
    assert not errs
    assert ep.inst_path == "top.d"
    assert ep.port_name == "w_e"
    assert ep.module == "D"
    assert batch.results[0].connected is True


def test_connectivity_port_inst_name_collision(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    lookup = {r.full_path: r for r in rows}
    _inject_spurious_child_inst(lookup, "top.d")
    result = check_connectivity(
        "top.d.w_e",
        "top.d.w_e",
        rows=rows,
        index=index,
        top="top",
    )
    lookup = {r.full_path: r for r in rows}
    _inject_spurious_child_inst(lookup, "top.d")
    result = check_connectivity(
        "top.d.w_e",
        "top.d.w_e",
        rows=rows,
        index=index,
        top="top",
        rows_by_path=lookup,
    )
    assert result.connected
    assert result.endpoint_a.inst_path == "top.d"
    assert result.endpoint_a.module == "D"