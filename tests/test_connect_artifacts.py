"""Two-phase connect artifacts: text-conn then logical-conn."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect_artifacts import (
    any_text_conn_hit,
    apply_connect_logical_phase,
    archive_run_config_sources,
    connect_output_paths,
    format_connect_hierarchy_tsv,
    merge_refined_connect_results,
    reorder_connect_results_to_checks,
    resolve_connect_output_dir,
    snapshot_connect_text_phase,
    prepare_text_connect_request,
    reorder_connect_checks_by_b_endpoint,
    verification_output_path,
    write_connect_phase_tsv,
)
from hierwalk.connectivity import format_connect_results_tsv
from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.run_request import RunConfig


def _tsv_rows(tsv: str) -> list[dict[str, str]]:
    lines = [ln for ln in tsv.strip().splitlines() if ln and not ln.startswith("#")]
    headers = lines[0].split("\t")
    return [dict(zip(headers, row.split("\t"))) for row in lines[1:]]


def test_connect_output_paths_under_db(tmp_path: Path):
    db = tmp_path / ".db_top"
    paths = connect_output_paths(db)
    assert paths.text_tsv == db / "conn.text.tsv"
    assert paths.logical_tsv == db / "conn.tsv"
    assert paths.hierarchy_text_tsv == db / "hierarchy.text.tsv"
    assert paths.hierarchy_logical_tsv == db / "hierarchy.tsv"


def test_connect_output_paths_custom_name(tmp_path: Path):
    db = tmp_path / ".db_top"
    paths = connect_output_paths(db, "results/conn.tsv")
    assert paths.text_tsv == db / "conn.text.tsv"
    assert paths.logical_tsv == db / "conn.tsv"
    assert paths.hierarchy_text_tsv == db / "hierarchy.text.tsv"
    assert paths.hierarchy_logical_tsv == db / "hierarchy.tsv"


def test_connect_output_paths_suite_name(tmp_path: Path):
    db = tmp_path / ".db_top"
    paths = connect_output_paths(db, "VERIFY_gate_conn.tsv")
    assert paths.hierarchy_logical_tsv == db / "VERIFY_gate_hierarchy.tsv"
    assert paths.hierarchy_text_tsv == db / "VERIFY_gate_hierarchy.text.tsv"


def test_archive_run_config_sources(tmp_path: Path):
    run_json = tmp_path / "RUN.json"
    run_json.write_text('{"filelist": "x.f"}\n', encoding="utf-8")
    db = tmp_path / ".db_soc"
    cfg = RunConfig(
        filelist=str(tmp_path / "x.f"),
        run_config_source=str(run_json),
    )
    archived = archive_run_config_sources(db, cfg)
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == run_json.read_text(encoding="utf-8")


def test_text_miss_skips_logical_notes():
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.miss", "top", "miss", "top"),
        endpoint_b=ConnectEndpoint("top.miss", "top", "miss", "top"),
        connected=False,
        mode="port-port",
        connected_text=False,
        errors=["hierarchy not found"],
    )
    apply_connect_logical_phase([result], {}, run_activation=True)
    assert result.connected_logical is False
    assert result.logical_notes == []


def test_all_text_miss_skips_activation_path():
    hit = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top", port_found=True),
        connected=True,
        mode="port-port",
        connected_text=True,
    )
    miss = ConnectResult(
        endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
        endpoint_b=ConnectEndpoint("top.y", "top", "y", "top"),
        connected=False,
        mode="unknown",
        connected_text=False,
    )
    assert any_text_conn_hit([hit, miss]) is True
    assert any_text_conn_hit([miss]) is False
    apply_connect_logical_phase([miss], {}, run_activation=False)
    assert miss.connected_logical is False
    assert miss.logical_notes == []


def test_logical_phase_uses_refined_not_text():
    """Logical conn must follow refined COI, not a rough text-conn pass."""
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top", port_found=True),
        connected=False,
        mode="port-port",
        connected_text=True,
    )
    apply_connect_logical_phase([result], {})
    assert result.connected_logical is False
    assert result.connected is False


def test_logical_phase_downgrades_provisional():
    row = FlatRow(
        full_path="top.u",
        parent_path="top",
        inst_leaf="u",
        module="CH",
        depth=1,
        file="ch.v",
        refine_status="provisional",
    )
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.u.a", "top.u", "a", "CH", port_found=True),
        endpoint_b=ConnectEndpoint("top.u.b", "top.u", "b", "CH", port_found=True),
        connected=True,
        mode="port-port",
        connected_text=True,
    )
    apply_connect_logical_phase([result], {"top.u": row})
    assert result.connected_logical is False
    assert any("provisional" in n for n in result.logical_notes)


def test_logical_phase_downgrades_inactive_ifdef():
    row = FlatRow(
        full_path="top.u",
        parent_path="top",
        inst_leaf="u",
        module="CH",
        depth=1,
        file="ch.v",
        refine_status="inactive_ifdef",
        activation="inactive",
    )
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.u.a", "top.u", "a", "CH", port_found=True),
        endpoint_b=ConnectEndpoint("top.u.b", "top.u", "b", "CH", port_found=True),
        connected=True,
        mode="port-port",
        connected_text=True,
    )
    apply_connect_logical_phase([result], {"top.u": row})
    assert result.connected_logical is False
    assert result.connected is False
    assert any("inactive" in n for n in result.logical_notes)


def test_path_walk_writes_text_and_logical_tsv(tmp_path: Path):
    (tmp_path / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp_path / "a.v").write_text(
        "module A; wire x; B u (.y(x)); endmodule\nmodule B(output y); assign y = 1'b0; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(str((tmp_path / n).resolve()) for n in ("top.v", "a.v")) + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    db = tmp_path / ".db_top"
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.a.x", "top.a.u.y"),),
        top="top",
    )
    batch, _index, _state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_output_dir=db,
    )
    text_path = db / "conn.text.tsv"
    logical_path = db / "conn.tsv"
    assert text_path.is_file()
    assert logical_path.is_file()
    text_rows = _tsv_rows(text_path.read_text(encoding="utf-8"))
    logical_rows = _tsv_rows(logical_path.read_text(encoding="utf-8"))
    assert text_rows[0]["phase"] == "text"
    assert logical_rows[0]["phase"] == "logical"
    assert "connected_text" in logical_rows[0]
    assert "connected_logical" in logical_rows[0]
    assert batch.results[0].connected_text is not None
    assert batch.results[0].connected_logical is not None
    hier_text = db / "hierarchy.text.tsv"
    hier_logical = db / "hierarchy.tsv"
    assert hier_text.is_file()
    assert hier_logical.is_file()
    hier_rows = _tsv_rows(hier_text.read_text(encoding="utf-8"))
    assert {row["side"] for row in hier_rows} <= {"a", "b", "?"}
    assert "a" in {row["side"] for row in hier_rows}
    assert "b" in {row["side"] for row in hier_rows}
    assert any(row["status"] == "hit" for row in hier_rows)
    assert any(row["kind"] in ("port", "wire", "signal") for row in hier_rows)


def test_hierarchy_port_rtl_uses_longest_walked_inst_prefix(tmp_path: Path):
    """Sliced port paths must inherit RTL from the parent inst row, not top."""
    top_v = tmp_path / "top.v"
    mid_v = tmp_path / "mid.v"
    top_v.write_text(
        "module top;\n"
        "  mid u ();\n"
        "endmodule\n",
        encoding="utf-8",
    )
    mid_v.write_text(
        "module mid(input logic [3:0] chain_in);\n"
        "  assign chain_in[0] = chain_in[1];\n"
        "endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(f"{top_v.resolve()}\n{mid_v.resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                "top.u.chain_in[0]",
                "top.u.chain_in[1]",
                check_id="bus",
            ),
        ),
        top="top",
    )
    batch, index, state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
    )
    body = format_connect_hierarchy_tsv(
        batch.results,
        state.rows_by_path,
        phase="text",
        index=index,
        top="top",
    )
    rows = _tsv_rows(body)
    port_hits = [
        r
        for r in rows
        if r["check_id"] == "bus" and r["kind"] == "port" and r["status"] == "hit"
    ]
    assert len(port_hits) == 2
    for row in port_hits:
        assert Path(row["rtl"]).name == "mid.v"
        assert row["module"] == "mid"


def test_compact_hierarchy_evidence_drops_redundant_inst_hits():
    from hierwalk.connect_artifacts import (
        HierarchyEvidenceRow,
        compact_hierarchy_evidence,
    )

    evidence = [
        HierarchyEvidenceRow("c1", "a", "inst", "top", "hit", "TOP"),
        HierarchyEvidenceRow("c1", "a", "inst", "top.a", "hit", "A"),
        HierarchyEvidenceRow("c1", "a", "inst", "top.a.b", "hit", "B"),
        HierarchyEvidenceRow("c1", "a", "wire", "top.a.b.sig", "hit", "B"),
    ]
    compact = compact_hierarchy_evidence(evidence)
    inst_paths = [r.path for r in compact if r.kind == "inst"]
    assert inst_paths == ["top.a.b"]
    assert any(r.kind == "wire" for r in compact)


def test_format_connect_hierarchy_tsv_marks_miss_prefixes():
    rows_by_path = {
        "top": FlatRow(
            full_path="top",
            inst_leaf="top",
            module="TOP",
            depth=0,
            parent_path=None,
            file="/rtl/top.v",
        ),
    }
    result = ConnectResult(
        check_id="c1",
        endpoint_a=ConnectEndpoint("top.missing.sig", "top", "missing", "TOP"),
        endpoint_b=ConnectEndpoint("top.clk", "top", "clk", "TOP", port_found=True),
        connected=False,
        mode="port-port",
    )
    tsv = format_connect_hierarchy_tsv([result], rows_by_path, phase="text")
    parsed = _tsv_rows(tsv)
    a_rows = [row for row in parsed if row["side"] == "a"]
    assert any(row["path"] == "top" and row["status"] == "hit" for row in a_rows)
    assert any(row["path"] == "top.missing" and row["status"] == "miss" for row in a_rows)
    b_signal = [
        row
        for row in parsed
        if row["side"] == "b" and row["kind"] in ("port", "wire", "signal")
    ]
    assert b_signal and b_signal[0]["status"] == "hit"


def test_resolve_connect_output_dir_falls_back_to_db_top(tmp_path: Path):
    db = tmp_path / ".db_soc"
    resolved = resolve_connect_output_dir(
        None,
        top="soc",
        cache_dir=db,
    )
    assert resolved == db.resolve()
    assert resolved.is_dir()


def test_path_walk_text_conn_writes_tsv_without_output_dir_or_active_work_dir(
    tmp_path: Path,
):
    """Text-conn step must leave conn.text.tsv even without execute_run work-dir setup."""
    (tmp_path / "top.v").write_text(
        "module top(input logic clk); child u0(.clk(clk)); endmodule\n"
        "module child(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{(tmp_path / 'top.v').resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.u0.clk"),),
        top="top",
    )
    run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
        cache_dir=tmp_path / ".db_top",
    )
    assert (tmp_path / ".db_top" / "conn.text.tsv").is_file()


def test_path_walk_text_conn_zero_checks_writes_header_tsv(tmp_path: Path):
    (tmp_path / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    fl = tmp_path / "fl.f"
    fl.write_text(f"{(tmp_path / 'top.v').resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    db = tmp_path / ".db_top"
    req = ConnectivityRequest(checks=(), top="top")
    run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_phase="text",
        connect_output_dir=db,
    )
    text_path = db / "conn.text.tsv"
    assert text_path.is_file()
    lines = [ln for ln in text_path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines[0] == "# connect results"
    assert any(ln.startswith("check_id\t") for ln in lines)
    data_rows = [
        ln
        for ln in lines
        if ln and not ln.startswith("#") and not ln.startswith("check_id\t")
    ]
    assert data_rows == []


def test_path_walk_connect_writes_tsv_via_active_work_dir(tmp_path: Path):
    """COI timing can finish without explicit connect_output_dir when work dir is active."""
    from hierwalk.cache import set_active_work_dir, top_work_dir

    (tmp_path / "top.v").write_text(
        "module top(input logic clk); child u0(.clk(clk)); endmodule\n"
        "module child(input logic clk); endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "fl.f"
    fl.write_text(f"{(tmp_path / 'top.v').resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    work = top_work_dir("top", base=tmp_path)
    set_active_work_dir(work)
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.clk", "top.u0.clk"),),
        top="top",
    )
    run_path_walk_connect(req, flr, top="top", no_cache=True, connect_phase="text")
    assert (work / "conn.text.tsv").is_file()


def test_recovery_runs_after_text_conn(tmp_path: Path):
    """Heavy recovery must not block conn.text.tsv (runs in logical-conn)."""
    import io

    from tests.test_path_walk_miss_recovery import _write_dup_blk_chain

    fl_path, leaf = _write_dup_blk_chain(tmp_path)
    flr = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    req = ConnectivityRequest(checks=(ConnectivityCheck(leaf, leaf),), top="SOC_TOP")
    run_path_walk_connect(
        req,
        flr,
        top="SOC_TOP",
        no_cache=True,
        trace_stream=buf,
    )
    text = buf.getvalue()
    text_done = text.find("connect-text-conn done")
    logical_walk = text.find("connect-logical-walk done")
    assert text_done > 0
    assert logical_walk > text_done


def test_write_connect_phase_tsv_roundtrip(tmp_path: Path):
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=True,
        mode="port-port",
        connected_text=True,
        connected_logical=True,
    )
    out = tmp_path / "conn.tsv"
    write_connect_phase_tsv(out, [result], phase="logical")
    rows = _tsv_rows(out.read_text(encoding="utf-8"))
    assert rows[0]["connected_text"] == "True"
    assert rows[0]["connected_logical"] == "True"
    assert rows[0]["connected"] == "True"


def test_reorder_connect_checks_by_b_endpoint():
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.a", "top.z", check_id="z"),
            ConnectivityCheck("top.a", "top.b", check_id="b"),
            ConnectivityCheck("top.c", "top.b", check_id="b2"),
        ),
        top="top",
    )
    out = reorder_connect_checks_by_b_endpoint(req)
    bs = [c.endpoint_b for c in out.checks]
    assert bs == ["top.b", "top.b", "top.z"]


def test_prepare_text_connect_request_is_stable():
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.miss", "top.b", check_id="m"),),
        top="top",
    )
    assert prepare_text_connect_request(req) is req


def test_merge_refined_connect_results_matches_by_check_id_not_index():
    text_a = ConnectResult(
        endpoint_a=ConnectEndpoint("top.z", "top", "z", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top", port_found=True),
        connected=True,
        mode="port-port",
        connected_text=True,
        check_id="second",
    )
    text_b = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.c", "top", "c", "top", port_found=True),
        connected=True,
        mode="port-port",
        connected_text=True,
        check_id="first",
    )
    log_a = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.c", "top", "c", "top", port_found=True),
        connected=False,
        mode="port-port",
        errors=["refined miss"],
        check_id="first",
    )
    log_b = ConnectResult(
        endpoint_a=ConnectEndpoint("top.z", "top", "z", "top", port_found=True),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top", port_found=True),
        connected=True,
        mode="port-port",
        check_id="second",
    )
    text_rows = [text_a, text_b]
    merge_refined_connect_results(text_rows, [log_a, log_b])
    assert text_a.endpoint_a.port_name == "z"
    assert text_a.connected is True
    assert text_a.connected_text is True
    assert text_b.endpoint_a.port_name == "a"
    assert text_b.connected is False
    assert text_b.connected_text is True
    assert text_b.errors == ["refined miss"]


def test_reorder_connect_results_to_checks_restores_request_order():
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.a", "top.b", check_id="first"),
            ConnectivityCheck("top.z", "top.y", check_id="second"),
        ),
        top="top",
    )
    results = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.z", "top", "z", "top"),
            endpoint_b=ConnectEndpoint("top.y", "top", "y", "top"),
            connected=True,
            mode="port-port",
            check_id="second",
        ),
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            check_id="first",
        ),
    )
    ordered = reorder_connect_results_to_checks(req.checks, results)
    assert [r.check_id for r in ordered] == ["first", "second"]


def test_default_verification_artifact_names():
    from hierwalk.connect_artifacts import default_verification_artifact_name

    assert default_verification_artifact_name("run_conn_check") == "conn.tsv"
    assert default_verification_artifact_name("run_io_trace") == "io_trace.tsv"
    assert default_verification_artifact_name("run_cone_trace") == "cone_trace.tsv"
    assert default_verification_artifact_name("run_on_full_index") == "instances.tsv"


def test_verification_output_path_text_suffix():
    assert verification_output_path("trace.tsv", "text").name == "trace.text.tsv"
    assert verification_output_path("trace.tsv", "logical").name == "trace.tsv"