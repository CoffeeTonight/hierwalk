"""Waypoint-qualified fanout trace (map.kind waypoint-fanout)."""

from __future__ import annotations

from pathlib import Path

import json

import pytest

from hierwalk.connect_expand import build_expand_meta, expand_check_to_pairs
from hierwalk.connect_request import (
    connect_request_to_json,
    parse_connect_request_json,
)
from hierwalk.connectivity import (
    ConnectivitySession,
    format_connect_results_tsv,
)
from hierwalk.connect_scan import build_module_connect_index, lookup_edge_prov
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.waypoint_fanout import (
    format_waypoint_fanout_tsv,
    run_waypoint_fanout_check,
)

WAYPOINT_RTL = """
module top(input logic clk, input logic drv, output logic z, output logic direct);
  wire mid;
  assign mid = drv;
  child u_c (.clk(clk), .din(mid), .qout(z));
  leaf u_leaf (.p(drv), .q(direct));
endmodule

module child(input logic clk, input logic din, output logic qout);
  logic r;
  always_ff @(posedge clk) r <= din;
  assign qout = r;
endmodule

module leaf(input logic p, output logic q);
  assign q = p;
endmodule
"""


def _elab(verilog: str, tmp_path: Path, top: str = "top"):
    rtl = tmp_path / "d.v"
    rtl.write_text(verilog, encoding="utf-8")
    index = DesignIndex.build({str(rtl): verilog})
    _, rows = elaborate(index, top)
    return index, rows, top


def test_connect_scan_edge_prov_assign_line(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    row = next(r for r in rows if r.module == "top")
    body = index.module_body("top")
    mod_idx = build_module_connect_index(body)
    prov = lookup_edge_prov(mod_idx, "mid", "drv")
    assert prov is not None
    assert prov.kind == "assign"
    assert prov.line > 0
    assert mod_idx.inst_stmt_lines.get("u_c", 0) > 0
    assert mod_idx.inst_stmt_lines.get("u_leaf", 0) > 0


def test_waypoint_port_and_inst(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    result, _ = run_waypoint_fanout_check(
        ["top.drv"],
        ["top.u_c", "top.u_c.din"],
        rows=rows,
        index=index,
        top=top,
        trace_interior=True,
    )
    events = list(result.waypoint_events)
    assert any(e.waypoint_hit == "Y" and e.event_kind == "child-down" for e in events)
    port_hits = [e for e in events if e.waypoint_hit == "Y" and e.net == "din"]
    assert port_hits


def test_waypoint_fanout_trace(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    expand = build_expand_meta(
        ["top.drv"],
        ["top.u_c"],
        map_spec={
            "kind": "waypoint-fanout",
            "path_kind": "comb",
            "trace_interior": True,
        },
    )
    session = ConnectivitySession(rows=rows, index=index, top=top)
    result = session.check(
        "top.drv",
        "top.u_c",
        expand=expand,
        check_id="wp",
    )

    assert result.mode == "waypoint-fanout"
    assert result.waypoint_events
    events = list(result.waypoint_events)

    assert any(e.rtl_line > 0 for e in events)

    child_down = [e for e in events if e.event_kind == "child-down"]
    assert child_down
    assert any(e.scope == "top.u_c" and e.rtl_line > 0 for e in child_down)
    assert any(e.waypoint_hit == "Y" for e in child_down)

    terminators = [e for e in events if e.is_terminator == "Y"]
    qualified = [e for e in terminators if e.waypoint_qualified == "Y"]
    unqualified = [e for e in terminators if e.waypoint_qualified != "Y"]
    assert qualified
    assert unqualified
    assert any("u_leaf" in e.scope for e in unqualified)
    assert any("u_c" in e.scope for e in qualified)

    tsv = format_waypoint_fanout_tsv(events)
    assert "rtl_line" in tsv
    assert "\tY\t" in tsv


def test_waypoint_ff_rtl_line_and_inst_hit_on_terminator(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    result, _ = run_waypoint_fanout_check(
        ["top.drv"],
        ["top.u_c"],
        rows=rows,
        index=index,
        top=top,
        path_kind="ff",
    )
    events = list(result.waypoint_events)
    ff_events = [e for e in events if e.event_kind.startswith("ff-")]
    assert ff_events
    assert any(e.rtl_line > 0 for e in ff_events)
    ff_u_c = [
        e
        for e in events
        if "u_c" in e.scope and e.event_kind.startswith("ff-")
    ]
    assert ff_u_c
    assert any(e.waypoint_hit == "Y" and e.rtl_line > 0 for e in ff_u_c)


def test_waypoint_assign_rtl_line(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    result, _ = run_waypoint_fanout_check(
        ["top.drv"],
        ["top.u_c"],
        rows=rows,
        index=index,
        top=top,
        trace_interior=True,
    )
    child_down = [e for e in result.waypoint_events if e.event_kind == "child-down"]
    assert child_down
    assert any(e.rtl_line > 0 for e in child_down)


def test_dual_fanout_direction_both(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    expand = build_expand_meta(
        ["top.drv"],
        ["top.u_c"],
        map_spec={"kind": "waypoint-fanout", "direction": "both", "path_kind": "ff"},
    )
    assert expand.direction == "both"
    session = ConnectivitySession(rows=rows, index=index, top=top)
    result = session.check(
        "[top.drv]",
        "[top.u_c]",
        expand=expand,
        check_id="dual",
    )
    events = list(result.waypoint_events)
    a_events = [e for e in events if e.side == "a-fanout"]
    b_events = [e for e in events if e.side == "b-fanout"]
    assert a_events
    assert b_events

    off_path = [
        e
        for e in events
        if e.is_terminator == "Y"
        and e.waypoint_hit == "N"
        and "u_leaf" in e.scope
    ]
    assert off_path
    assert off_path[0].scope
    assert off_path[0].net

    peer_terms = [
        e
        for e in events
        if e.is_terminator == "Y" and e.waypoint_hit == "Y" and "u_c" in e.scope
    ]
    assert peer_terms
    assert any(e.peer_matched != "-" for e in peer_terms)

    tsv = format_waypoint_fanout_tsv(events)
    assert "side" in tsv
    assert "peer_matched" in tsv
    assert "direction=both" in (result.note or "")


def test_waypoint_fanout_json_round_trip():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "wp",
                    "a": ["top.drv"],
                    "b": ["top.u_c"],
                    "map": {"kind": "waypoint-fanout", "path_kind": "ff"},
                }
            ]
        }
    )
    again = parse_connect_request_json(json.loads(connect_request_to_json(req)))
    chk = again.checks[0]
    assert chk.expand is not None
    assert chk.expand.map_kind == "waypoint-fanout"
    assert chk.expand.path_kind == "ff"


def test_waypoint_fanout_json_round_trip_direction_both():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "dual",
                    "a": ["top.a"],
                    "b": ["top.b"],
                    "map": {
                        "kind": "waypoint-fanout",
                        "direction": "both",
                    },
                }
            ]
        }
    )
    again = parse_connect_request_json(json.loads(connect_request_to_json(req)))
    assert again.checks[0].expand.direction == "both"


def test_expand_check_to_pairs_rejects_waypoint_fanout():
    expand = build_expand_meta(
        ["top.a"],
        ["top.b"],
        map_spec={"kind": "waypoint-fanout"},
    )
    with pytest.raises(ValueError, match="waypoint-fanout"):
        expand_check_to_pairs("top.a", "top.b", expand=expand)


def test_waypoint_fanout_path_kind_array(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    expand = build_expand_meta(
        ["top.drv"],
        ["top.u_c"],
        map_spec={
            "kind": "waypoint-fanout",
            "path_kind": ["ff", "comb"],
            "full_path_kinds": True,
            "trace_interior": True,
        },
    )
    assert expand.path_kinds == ("ff", "comb")
    session = ConnectivitySession(rows=rows, index=index, top=top)
    result = session.check(
        "top.drv",
        "top.u_c",
        expand=expand,
        check_id="wp_multi",
    )
    events = list(result.waypoint_events)
    ff_events = [e for e in events if e.path_kind == "ff"]
    comb_events = [e for e in events if e.path_kind == "comb"]
    assert ff_events
    assert comb_events
    assert any(e.event_kind == "ff-interior" for e in ff_events)
    assert not any(e.event_kind == "ff-interior" for e in comb_events)

    tsv = format_waypoint_fanout_tsv(events)
    assert "path_kind" in tsv


def test_waypoint_fanout_path_kind_array_json_round_trip():
    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "wp",
                    "a": ["top.drv"],
                    "b": ["top.u_c"],
                    "map": {
                        "kind": "waypoint-fanout",
                        "path_kind": ["ff", "comb"],
                    },
                }
            ]
        }
    )
    again = parse_connect_request_json(json.loads(connect_request_to_json(req)))
    chk = again.checks[0]
    assert chk.expand is not None
    assert chk.expand.path_kinds == ("ff", "comb")
    payload = json.loads(connect_request_to_json(req))
    assert payload["checks"][0]["map"]["path_kind"] == ["ff", "comb"]


def test_waypoint_fanout_tsv_in_connect_output(tmp_path: Path):
    index, rows, top = _elab(WAYPOINT_RTL, tmp_path)
    result, _ = run_waypoint_fanout_check(
        ["top.drv"],
        ["top.u_c"],
        rows=rows,
        index=index,
        top=top,
        check_id="wp2",
        endpoint_a="top.drv",
        endpoint_b="top.u_c",
    )
    tsv = format_connect_results_tsv([result])
    assert "waypoint-fanout trace" in tsv
    assert "waypoint_qualified" in tsv