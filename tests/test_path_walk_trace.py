"""Path-walk per-node rtl + filelist trace logging."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.hierarchy_log import format_path_walk_spine_lines, path_walk_trace_show_message
from hierwalk.models import FlatRow
from hierwalk.path_walk import build_path_walk_state_from_specs


def _row(path: str, *, file: str, via: str, chain: str) -> FlatRow:
    parts = path.split(".")
    return FlatRow(
        full_path=path,
        inst_leaf=parts[-1],
        module=parts[-1],
        depth=len(parts) - 1,
        parent_path=".".join(parts[:-1]) if len(parts) > 1 else None,
        file=file,
        via_filelist=via,
        filelist_chain=chain,
    )


def test_path_walk_trace_filter_hides_search_keeps_hits():
    assert not path_walk_trace_show_message("walk target=top.u_a")
    assert not path_walk_trace_show_message("pw-db tier0 scan a.v -> A")
    assert not path_walk_trace_show_message("pw-db tier1 scan a.v -> A(1inst)")
    assert not path_walk_trace_show_message("pw-db edge B.C candidates=2")
    assert path_walk_trace_show_message(
        "pw-db inst-resolve enter SOC_TOP.u_ip file=allinst.v policy=confident"
    )
    assert path_walk_trace_show_message(
        "pw-db inst-resolve tier1-probe miss SOC_TOP.u_ip edges=12000 "
        "tier1_ms=45000.0 since_enter_ms=45001.2"
    )
    assert path_walk_trace_show_message(
        "pw-db inst-find enter SOC_TOP.u_ip file=allinst.v pre_ms=45001.2"
    )
    assert path_walk_trace_show_message(
        "pw-db inst-find enter SOC_TOP.u_ip file=allinst.v"
    )
    assert path_walk_trace_show_message(
        "pw-db inst-find done SOC_TOP.u_ip file=allinst.v hit ms=12.3 "
        "preprocess=10.0 module_body=0.1 inst_scan=2.2 body_chars=50000"
    )
    assert path_walk_trace_show_message(
        "pw-db tier1-scan done allinst.v source=cold ms=120000.0 chars=3451958"
    )
    assert path_walk_trace_show_message("connect-coi done checks=1 modules_cached=3 ms=45000.0")
    assert path_walk_trace_show_message(
        "connect-comb build module=TOP body_chars=3451958 insts=12 ms=8000.0"
    )
    assert path_walk_trace_show_message(
        "walk raw-inst-probe scope=TOP.u_x leaf='u_y' hit=False rtl=allinst.v ms=1200.0"
    )
    assert path_walk_trace_show_message("pw-db preprocess enter allinst.v")
    assert path_walk_trace_show_message(
        "pw-db preprocess done allinst.v source=cold ms=9000.0 chars=120000"
    )
    assert not path_walk_trace_show_message("pw-db   edge miss b.v: no inst 'C'")
    assert not path_walk_trace_show_message("pw-db tier0 expand edge B.C +1 file(s)")
    assert not path_walk_trace_show_message("pw-db v3 root=/cache module_map=3")
    assert path_walk_trace_show_message("ok top.u_a  module=mid  rtl=/rtl/mid.v")
    assert path_walk_trace_show_message("pw-db   hit b.v for module 'B'")
    assert path_walk_trace_show_message("pw-db   edge hit B.C via b.v -> child 'C'")
    assert path_walk_trace_show_message("miss inst=C under A.B (instance edge not found)")
    assert path_walk_trace_show_message(
        "parallel fork at=top.u_soc jobs=4 pool=2 branches=u_cpusystem,u_ifdef"
    )
    assert path_walk_trace_show_message(
        "parallel worker j=1/2 branch=u_cpusystem from=top.u_soc specs=3"
    )
    assert path_walk_trace_show_message(
        "signal-tail hit kind=wire scope=top.u_ifdef tail='c' target=top.u_ifdef.c "
        "module=mid_ifdef rtl=soc.v lines=42 check_ms=1.2"
    )


def test_path_walk_spine_lines_include_filelist():
    rows = {
        "top": _row("top", file="/rtl/top.v", via="/lists/a.f", chain="/lists/a.f"),
        "top.u_mid": _row(
            "top.u_mid",
            file="/rtl/mid.v",
            via="/lists/b.f",
            chain="/lists/a.f > /lists/b.f",
        ),
    }
    lines = format_path_walk_spine_lines("top.u_mid", rows)
    joined = "\n".join(lines)
    assert "rtl=/rtl/top.v" in joined
    assert "via_filelist=/lists/a.f" in joined
    assert "rtl=/rtl/mid.v" in joined
    assert "filelist_chain=/lists/a.f > /lists/b.f" in joined


def test_path_walk_trace_logs_nodes_and_miss(tmp_path: Path):
    top_v = tmp_path / "top.v"
    top_v.write_text(
        """
        module top;
          // no children
        endmodule
        """,
        encoding="utf-8",
    )
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{top_v.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    listing = str(fl_path.resolve())
    from hierwalk.path_walk import create_path_walk_index

    index, mod_db = create_path_walk_index(fl, "top", defines={})
    buf = io.StringIO()
    build_path_walk_state_from_specs(
        index,
        "top",
        ["top.u_missing"],
        mod_db,
        trace_stream=buf,
    )
    text = buf.getvalue()
    assert "[hier-walk path-walk]" in text
    assert "walk target=" not in text
    assert "pw-db tier0" not in text
    assert "ok top" in text
    assert "rtl=" in text
    assert "via_filelist=" in text
    assert "miss inst=u_missing under top" in text
    assert "walked" in text


def test_path_walk_connect_trace_writes_pw_db_to_run_log(tmp_path: Path):
    a_v = tmp_path / "a.v"
    a_v.write_text("module A; B B (); endmodule\n", encoding="utf-8")
    b_v = tmp_path / "b.v"
    b_v.write_text("module B; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{a_v.resolve()}\n{b_v.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
    from hierwalk.path_walk import run_path_walk_connect

    log_path = tmp_path / "out.tsv.hier-walk.log"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("A.B", "A.B"),),
        top="A",
    )
    run_path_walk_connect(
        request,
        fl,
        top="A",
        no_cache=True,
        trace_log_path=log_path,
    )
    text = log_path.read_text(encoding="utf-8")
    assert "# path-walk trace" in text
    assert "pw-db tier0" not in text
    assert "pw-db tier1 scan " not in text
    assert "connect-coi done" in text
    assert "ms=" in text
    assert "ok A" in text
    assert "[hier-walk path-walk]" in text


def test_path_walk_trace_writes_run_log(tmp_path: Path):
    top_v = tmp_path / "top.v"
    top_v.write_text(
        """
        module top;
        endmodule
        """,
        encoding="utf-8",
    )
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{top_v.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.path_walk import create_path_walk_index

    index, mod_db = create_path_walk_index(fl, "top", defines={})
    log_path = tmp_path / "out.tsv.hier-walk.log"
    build_path_walk_state_from_specs(
        index,
        "top",
        ["top.u_missing"],
        mod_db,
        trace_log_path=log_path,
    )
    text = log_path.read_text(encoding="utf-8")
    assert "# path-walk trace" in text
    assert "[hier-walk path-walk]" in text
    assert "ok top" in text
    assert "rtl=" in text
    assert "via_filelist=" in text