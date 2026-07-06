"""Wire vs instance leaf name collision (e.g. ``wire u_b; B u_b (...)``)."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.endpoints import parse_connect_endpoint, resolve_endpoint
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk import (
    PathWalkState,
    _walk_target_from_spec,
    create_path_walk_index,
    run_path_walk_connect,
)


def _design() -> str:
    return """
    module top;
      A u_a ();
    endmodule
    module A;
      wire u_b;
      B u_b ();
    endmodule
    module B;
      wire probe;
    endmodule
    """


def _index_and_lookup(v: str, tmp_path: Path):
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    lookup = {r.full_path: r for r in rows}
    return index, lookup


def test_walk_target_prefers_inst_over_parent_wire(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index, mod_db = create_path_walk_index(flr, "top", defines={}, no_cache=True)
    state = PathWalkState(index=index, top="top", mod_db=mod_db)
    state.ensure_root()
    state.ensure_path("top.u_a")

    assert _walk_target_from_spec("top.u_a.u_b", state) == "top.u_a.u_b"
    assert "top.u_a.u_b" not in state.rows_by_path


def test_parse_connect_endpoint_prefers_inst_over_parent_wire(tmp_path: Path):
    index, lookup = _index_and_lookup(_design(), tmp_path)
    partial = {p: r for p, r in lookup.items() if p in ("top", "top.u_a")}

    hier, tail = parse_connect_endpoint(
        "top.u_a.u_b",
        partial,
        index=index,
        top="top",
    )
    assert hier == "top.u_a.u_b"
    assert tail is None


def test_path_walk_reaches_inst_when_wire_name_collides(tmp_path: Path):
    v = _design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.u_b.probe", "top.u_a.u_b.probe"),),
        top="top",
    )
    batch, index, state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
    )
    assert "top.u_a.u_b" in state.rows_by_path
    ep, errs = resolve_endpoint(
        "top.u_a.u_b.probe",
        state.rows(),
        index,
        top="top",
        rows_by_path=state.rows_by_path,
    )
    assert not errs
    assert ep.inst_path == "top.u_a.u_b"
    assert ep.port_name == "probe"
    assert ep.port_found
    assert batch.results[0].connected is True