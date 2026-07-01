"""COI must tolerate parametric bus bounds like ``[STRB_MAX-1:0]``."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_scan import _md_suffixes_for_token, build_module_connect_index
from hierwalk.connectivity import check_connectivity
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


def test_md_suffixes_parametric_bus_range_does_not_raise():
    suffixes = _md_suffixes_for_token(
        "data[STRB_MAX-1:0]",
        {"STRB_MAX": "4"},
        {},
        {},
    )
    assert suffixes is not None
    assert suffixes


def test_build_module_connect_index_parametric_port_width(tmp_path: Path):
    v = """
    module pkg;
      localparam int STRB_MAX = 4;
    endmodule
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    row = {r.full_path: r for r in rows}["top"]
    body = index.module_body("top")
    idx = build_module_connect_index(body, param_map={"STRB_MAX": "4"})
    assert "w_e" in idx.net_rep or any("w_e" in k for k in idx.net_rep)


def test_parametric_braced_concat_text_conn_bit_precise(tmp_path: Path):
    """Parametric ``[W-1:0]`` bus + ``{wa,wb,wc}`` must not collapse bits in text-conn."""
    v = """
    module top #(
      parameter int W = 3
    )();
      wire wa, wb, wc, wq;
      wire [W-1:0] bus;
      assign bus = {wa, wb, wc};
      assign wq = wb;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connect_request import parse_connect_request_json
    from hierwalk.connectivity import ConnectivitySession

    req = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ],
            "top": "top",
        }
    )
    session = ConnectivitySession(rows=rows, index=index, top="top")
    session.resolve_param_dims = False
    batch = session.run_text_request(req)
    parent = batch.results[0]
    by_pair = {
        (sr.endpoint_a.spec, sr.endpoint_b.spec): sr.connected
        for sr in parent.sub_results
    }
    assert by_pair[("top.wa", "top.wq")] is False
    assert by_pair[("top.wc", "top.wq")] is False
    assert by_pair[("top.wb", "top.wq")] is True


def test_text_conn_port_or_expr_does_not_bridge_operands(tmp_path: Path):
    """``.din(a | b)`` must not let ``a`` reach unrelated nets only wired to ``b``."""
    v = """
    module child(input logic din);
    endmodule
    module top;
      wire a, b, q;
      child u (.din(a | b));
      assign q = b;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.a", "top.q").connected is False
    assert session.check("top.b", "top.q").connected is True


def test_text_conn_port_xor_expr_does_not_bridge_operands(tmp_path: Path):
    """XOR operands must not reach each other or unrelated nets via parent-up bloom."""
    v = """
    module zz_bridge_ping(input logic [1:0][2:0] din, output logic [1:0][2:0] dout);
    endmodule
    module top;
      wire [1:0][2:0] chain_in, shallow_return, q;
      zz_bridge_ping u (.din(chain_in ^ shallow_return), .dout(q));
      assign q = shallow_return;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.chain_in[1][2]", "top.q[1][2]").connected is False
    assert session.check("top.chain_in[1][2]", "top.shallow_return[1][2]").connected is False
    assert session.check("top.shallow_return[1][2]", "top.q[1][2]").connected is True


def test_text_conn_port_xor_expr_keeps_operand_to_port(tmp_path: Path):
    """XOR port maps still connect each operand to the child port (parent-up path)."""
    v = """
    module zz_bridge_ping(input logic [1:0][2:0] din, output logic [1:0][2:0] dout);
    endmodule
    module top;
      wire [1:0][2:0] chain_in, shallow_return, expr_mapped;
      zz_bridge_ping u (.din(chain_in ^ shallow_return), .dout(expr_mapped));
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.chain_in[1][2]", "top.u.din[1][2]").connected is True
    assert session.check("top.shallow_return[1][2]", "top.u.din[1][2]").connected is True


def test_text_conn_scalar_port_map_survives_slice_or_assign(tmp_path: Path):
    """Whole-bus inst port maps must stay reachable when a slice OR assign references the bus."""
    v = """
    module zz_y_fork (
      input  logic [2:0][3:0] din,
      output logic [2:0][3:0] main_out,
      output logic [2:0][3:0] decoy_out
    );
      assign main_out = din;
      assign decoy_out = 12'b0;
    endmodule
    module top;
      logic [2:0][3:0] chain_in, shallow_return, fork_main, fork_decoy;
      logic merge_tap;
      zz_y_fork u (
        .din(chain_in),
        .main_out(fork_main),
        .decoy_out(fork_decoy)
      );
      assign merge_tap = fork_main[1][2] | shallow_return[1][2];
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.chain_in[1][2]", "top.fork_main[1][2]").connected is True
    assert session.check("top.fork_main[1][2]", "top.merge_tap").connected is True
    assert session.check("top.shallow_return[1][2]", "top.merge_tap").connected is True
    assert session.check("top.chain_in[1][2]", "top.merge_tap").connected is True


def test_text_conn_md_bus_slice_bit_precise(tmp_path: Path):
    """Literal ``[i][j]`` assigns must not bloom-unwire other MD elements in text-conn."""
    v = """
    module top;
      logic [1:0][2:0] a, b;
      assign b[0][1] = a[0][1];
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.a[0][1]", "top.b[0][1]").connected is True
    assert session.check("top.a[0][2]", "top.b[0][2]").connected is False


def test_text_conn_indexed_concat_port_map_bit_precise(tmp_path: Path):
    """``{sig[i][j] | ...}`` concat port maps must not be misclassified as compound."""
    v = """
    module child(input logic [1:0] din);
    endmodule
    module top;
      wire [1:0][2:0] chain_in, shallow_return, tap;
      child u (.din({chain_in[1][2] | shallow_return[1][2], chain_in[0][0]}));
      assign tap[1][2] = shallow_return[1][2];
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.chain_in[1][2]", "top.u.din[0]").connected is True
    assert session.check("top.shallow_return[1][2]", "top.u.din[0]").connected is True
    assert session.check("top.chain_in[0][0]", "top.u.din[1]").connected is True
    assert session.check("top.chain_in[1][2]", "top.tap[1][2]").connected is False
    assert session.check("top.shallow_return[1][2]", "top.tap[1][2]").connected is True


def test_text_conn_hier_port_concat_bit_precise(tmp_path: Path):
    """Instance ``.bus({wa,wb,wc})`` must not parent-up all concat legs at once."""
    v = """
    module child(input logic [2:0] bus);
    endmodule
    module top;
      wire wa, wb, wc, wq;
      child u (.bus({wa, wb, wc}));
      assign wq = wb;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    assert session.check("top.wa", "top.wq").connected is False
    assert session.check("top.wc", "top.wq").connected is False
    assert session.check("top.wb", "top.wq").connected is True


def test_text_conn_skips_parametric_dim_resolution(tmp_path: Path):
    """Text-conn (structural) must not evaluate ``STRB_MAX`` — decl/assign only."""
    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    text_session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    text_result = text_session.check("top.data", "top.w_e")
    assert not any("STRB_MAX" in err for err in text_result.errors)
    assert text_result.connected

    logical_session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=True,
    )
    logical_result = logical_session.check("top.data[0]", "top.w_e[0]")
    assert logical_result.connected


def test_text_conn_bloom_filter_accepts_slice_when_bus_connected(tmp_path: Path):
    """Text-conn treats slice endpoints as base-signal bloom hits (no dim resolve)."""
    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    from hierwalk.connectivity import ConnectivitySession

    text_session = ConnectivitySession(
        rows=rows,
        index=index,
        top="top",
        resolve_param_dims=False,
    )
    text_result = text_session.check("top.data[3]", "top.w_e[3]")
    assert not any("STRB_MAX" in err for err in text_result.errors)
    assert text_result.connected


def test_refine_param_ctx_for_top_only_path(tmp_path: Path):
    from hierwalk.path_refine import refine_param_ctx_for_path

    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk
    );
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    result = refine_param_ctx_for_path(index, "top", "top")
    assert result.ok
    assert result.param_ctx.get("STRB_MAX") == "4"


def test_port_param_ctx_refines_path_walk_rows_when_resolve_param_dims(tmp_path: Path):
    """Path-walk folded rows with empty ctx must refine under logical COI."""
    from hierwalk.connect_endpoints import _port_param_ctx
    from hierwalk.filelist import parse_filelist
    from hierwalk.models import FlatRow
    from hierwalk.path_walk import create_path_walk_index, build_path_walk_state
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    fl = tmp_path / "design.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.data[1]", "top.w_e[1]"),),
        top="top",
    )
    index, mod_db = create_path_walk_index(flr, "top", defines={}, no_cache=True)
    state = build_path_walk_state(index, "top", req, mod_db)
    row = state.rows_by_path["top"]
    row = FlatRow(
        full_path=row.full_path,
        inst_leaf=row.inst_leaf,
        module=row.module,
        depth=row.depth,
        parent_path=row.parent_path,
        file=row.file,
        param_ctx={},
        param_ctx_folded=True,
    )
    fast = _port_param_ctx(index, row, "top", resolve_param_dims=False)
    assert fast == {}
    logical = _port_param_ctx(index, row, "top", resolve_param_dims=True)
    assert logical.get("STRB_MAX") == "4"


def test_connectivity_parametric_bus_endpoints(tmp_path: Path):
    v = """
    module top #(
      parameter int STRB_MAX = 4
    )(
      input logic clk,
      input logic [STRB_MAX-1:0] data
    );
      wire [STRB_MAX-1:0] w_e;
      assign w_e = data;
    endmodule
    """
    rtl = tmp_path / "top.v"
    rtl.write_text(v, encoding="utf-8")
    index = DesignIndex.build({str(rtl): v})
    _, rows = elaborate(index, "top")
    result = check_connectivity(
        "top.data[0]",
        "top.w_e[0]",
        rows=rows,
        index=index,
        top="top",
    )
    assert result.connected