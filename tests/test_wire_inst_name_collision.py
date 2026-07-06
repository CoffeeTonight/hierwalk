"""Wire vs instance leaf name collision (e.g. ``wire u_b; B u_b (...)``)."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.endpoints import (
    classify_signal_tail_kind,
    parse_connect_endpoint,
    resolve_endpoint,
)
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


def _same_cell_design() -> str:
    return """
    module top;
      A u_a ();
    endmodule
    module A;
      wire u_b;
      u_b u_b (.x());
    endmodule
    module u_b;
      wire x;
    endmodule
    """


def test_module_body_cache_is_per_module_not_per_file(tmp_path: Path):
    """Regression: one RTL file with many modules must not share one body cache slot."""
    v = _same_cell_design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index, mod_db = create_path_walk_index(flr, "top", defines={}, no_cache=True)
    state = PathWalkState(index=index, top="top", mod_db=mod_db)
    state.ensure_root()
    top_row = state.rows_by_path["top"]
    state.ensure_path("top.u_a")
    a_row = state.rows_by_path["top.u_a"]

    top_body = state._cached_module_body(top_row)
    a_body = state._cached_module_body(a_row)
    assert "module top" in top_body or "u_a" in top_body
    assert "wire u_b" in a_body
    assert "u_b u_b" in a_body
    assert top_body != a_body
    assert classify_signal_tail_kind(
        index,
        a_row,
        "u_b",
        top="top",
        body=a_body,
    ) is None


def test_text_grep_uses_per_module_body_cache(tmp_path: Path):
    from hierwalk.connect.text.index import module_body_for_text_grep

    v = _same_cell_design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index, mod_db = create_path_walk_index(flr, "top", defines={}, no_cache=True)
    state = PathWalkState(index=index, top="top", mod_db=mod_db)
    state.ensure_root()
    state.ensure_path("top.u_a")
    a_row = state.rows_by_path["top.u_a"]
    state._cached_module_body(state.rows_by_path["top"])
    state._cached_module_body(a_row)

    body = module_body_for_text_grep(
        index,
        "A",
        module_body_cache=state._module_body_cache,
    )
    assert "wire u_b" in body
    assert "u_b u_b" in body


def test_path_walk_same_cell_inst_after_top_body_cached(tmp_path: Path):
    v = _same_cell_design()
    rtl = tmp_path / "d.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_a.u_b.x", "top.u_a.u_b.x"),),
        top="top",
    )
    batch, index, state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
    )
    assert "top.u_a.u_b" in state.rows_by_path
    assert batch.results[0].connected is True