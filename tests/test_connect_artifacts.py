"""Two-phase connect artifacts: text-conn then logical-conn."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.pipeline.artifacts import (
    HierarchyEvidenceRow,
    IncrementalHierarchyTsvWriter,
    any_text_conn_hit,
    apply_connect_logical_phase,
    apply_logical_coi_failure_to_results,
    apply_text_verdicts_to_results,
    archive_run_config_sources,
    build_hierarchy_row_context,
    build_logical_connect_request,
    load_text_connect_results_from_tsv,
    connect_output_paths,
    format_connect_hierarchy_tsv,
    merge_refined_connect_results,
    reorder_connect_results_to_checks,
    resolve_connect_output_dir,
    resolve_hierarchy_row_identity,
    snapshot_connect_text_phase,
    prepare_text_connect_request,
    reorder_connect_checks_by_b_endpoint,
    verification_output_path,
    write_connect_phase_tsv,
)
from hierwalk.connect.session import ConnectivitySession
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.connect.session import format_connect_results_tsv
from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
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


def test_logical_only_skips_text_miss_via_prior_tsv(tmp_path: Path, monkeypatch):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module top;
          wire hit_a, hit_b, miss_a, miss_b;
          assign hit_b = hit_a;
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    db = tmp_path / ".db_top"
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.hit_a", "top.hit_b", check_id="hit"),
            ConnectivityCheck("top.miss_a", "top.miss_b", check_id="miss"),
        ),
        top="top",
    )
    run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_output_dir=db,
        connect_phase="text",
    )
    run_calls: list[int] = []
    orig = ConnectivitySession.run_request

    def spy_run(self, request, **kwargs):
        run_calls.append(len(request.checks))
        return orig(self, request, **kwargs)

    monkeypatch.setattr(ConnectivitySession, "run_request", spy_run)
    batch, _, _state = run_path_walk_connect(
        req,
        flr,
        top="top",
        no_cache=True,
        connect_output_dir=db,
        connect_phase="logical",
    )
    assert run_calls == [1]
    by_id = {r.check_id: r for r in batch.results}
    assert by_id["hit"].connected is True
    assert by_id["miss"].connected is False
    assert by_id["miss"].connected_text is False


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


def test_endpoint_match_strength_rejects_shared_tail_across_scopes():
    from hierwalk.connect.pipeline.artifacts import (
        _endpoint_match_strength,
        _match_signal_tail_to_check,
    )
    from hierwalk.models import ConnectEndpoint, ConnectResult

    top_clk = ConnectEndpoint(
        "zz_torture_top.clk",
        "zz_torture_top",
        "clk",
        "zz_torture_top",
        port_found=True,
    )
    deep_clk = ConnectEndpoint(
        "zz_torture_top.u_zigzag.u_deep.d1.d2.d3.d4.d5.clk",
        "zz_torture_top.u_zigzag.u_deep.d1.d2.d3.d4.d5",
        "clk",
        "zz_deep_d5",
        port_found=True,
    )
    deep_tail = "zz_torture_top.u_zigzag.u_deep.d1.d2.d3.d4.d5.clk"
    assert _endpoint_match_strength(top_clk, deep_tail) == 0
    assert _endpoint_match_strength(deep_clk, deep_tail) == 100
    result = ConnectResult(
        check_id="zz_clk_deep",
        endpoint_a=top_clk,
        endpoint_b=deep_clk,
        connected=True,
        mode="port-port",
    )
    assert _match_signal_tail_to_check(deep_tail, result) == ("b", 100)


def test_collect_hierarchy_evidence_skips_unmatched_signal_tails():
    from hierwalk.connect.pipeline.artifacts import (
        SignalTailRecord,
        collect_hierarchy_evidence,
    )
    from hierwalk.models import ConnectEndpoint, ConnectResult, FlatRow

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
        check_id="c0",
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "TOP", port_found=True),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "TOP", port_found=True),
        connected=False,
        mode="",
    )
    tails = [
        SignalTailRecord(
            target_path="top.unrelated",
            parent_path="top",
            tail="unrelated",
            kind="port",
            hit=True,
            module="TOP",
        )
    ]
    evidence = collect_hierarchy_evidence(
        [result],
        rows_by_path,
        signal_tails=tails,
    )
    assert not any(row.check_id == "" for row in evidence)
    assert not any(row.side == "-" for row in evidence)


def test_hierarchy_evidence_for_check_dedup_shared_b_endpoint(tmp_path: Path):
    from hierwalk.connect.pipeline.artifacts import collect_hierarchy_evidence_for_check
    from hierwalk.connect.shared.request import ConnectivityCheck
    from hierwalk.elab import elaborate
    from hierwalk.index import DesignIndex

    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module top;\n"
        "  wire wa, wb, wc, wq;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    text = rtl.read_text(encoding="utf-8")
    index = DesignIndex.build({str(rtl): text})
    _, rows = elaborate(index, "top")
    rows_by_path = {row.full_path: row for row in rows}
    from hierwalk.connect.shared.request import parse_connect_request_json

    chk = parse_connect_request_json(
        {
            "checks": [
                {
                    "id": "fan",
                    "a": ["top.wa", "top.wc", "top.wb"],
                    "b": "top.wq",
                }
            ]
        }
    ).checks[0]
    evidence = collect_hierarchy_evidence_for_check(
        chk,
        rows_by_path,
        rows=rows,
        index=index,
        top="top",
    )
    b_wq_ids = {
        row.check_id
        for row in evidence
        if row.side == "b" and row.path.endswith(".wq")
    }
    assert b_wq_ids == {"fan->0", "fan->1", "fan->2"}
    b_wire_wq = [
        row
        for row in evidence
        if row.side == "b"
        and row.kind == "wire"
        and row.path.endswith(".wq")
    ]
    assert len(b_wire_wq) == 3


def test_compact_hierarchy_final_tsv_keeps_deepest_hit_only(tmp_path: Path):
    from hierwalk.connect.pipeline.artifacts import (
        collect_hierarchy_evidence,
        compact_hierarchy_evidence,
        write_hierarchy_evidence_tsv,
    )

    out = tmp_path / "hierarchy.text.tsv"
    rows_by_path = {
        "top": FlatRow(
            full_path="top",
            inst_leaf="top",
            module="TOP",
            depth=0,
            parent_path=None,
            file="/rtl/top.v",
        ),
        "top.u_leaf": FlatRow(
            full_path="top.u_leaf",
            inst_leaf="u_leaf",
            module="leaf",
            depth=1,
            parent_path="top",
            file="/rtl/leaf.v",
        ),
    }
    result = ConnectResult(
        check_id="c0",
        endpoint_a=ConnectEndpoint("top.u_leaf.w", "top.u_leaf", "w", "leaf"),
        endpoint_b=ConnectEndpoint("top.clk", "top", "clk", "TOP", port_found=True),
        connected=False,
        mode="",
    )
    evidence = compact_hierarchy_evidence(
        collect_hierarchy_evidence([result], rows_by_path)
    )
    write_hierarchy_evidence_tsv(out, evidence, phase="text", compact=False)
    body = out.read_text(encoding="utf-8")
    a_rows = [ln for ln in body.splitlines() if ln.startswith("c0\ta\t")]
    b_rows = [ln for ln in body.splitlines() if ln.startswith("c0\tb\t")]
    assert len(a_rows) == 1 and "top.u_leaf.w" in a_rows[0]
    assert len(b_rows) == 1 and "top.clk" in b_rows[0]


def test_resolve_hierarchy_row_identity_side():
    from hierwalk.connect.shared.request import ConnectivityCheck

    ctx = build_hierarchy_row_context(
        ConnectivityCheck("top.u_a.sig", "top.clk", check_id="t")
    )
    assert resolve_hierarchy_row_identity(ctx, "top.u_a") == ("t", "a")
    assert resolve_hierarchy_row_identity(ctx, "top.clk") == ("t", "b")


def test_compact_hierarchy_evidence_one_final_row_per_side():
    from hierwalk.connect.pipeline.artifacts import (
        HierarchyEvidenceRow,
        compact_hierarchy_evidence,
    )

    evidence = [
        HierarchyEvidenceRow("c1", "a", "inst", "top", "hit", "TOP"),
        HierarchyEvidenceRow("c1", "a", "inst", "top.u", "hit", "U"),
        HierarchyEvidenceRow("c1", "a", "wire", "top.u.sig", "miss", "U"),
        HierarchyEvidenceRow("c1", "b", "inst", "top", "hit", "TOP"),
        HierarchyEvidenceRow("c1", "b", "port", "top.clk", "hit", "TOP"),
    ]
    compact = compact_hierarchy_evidence(evidence)
    assert len(compact) == 2
    by_side = {row.side: row for row in compact}
    assert by_side["a"].path == "top.u.sig"
    assert by_side["b"].path == "top.clk"


def test_compact_hierarchy_evidence_with_preferred_paths_one_row_per_side():
    from hierwalk.connect.pipeline.artifacts import (
        HierarchyEvidenceRow,
        _preferred_hierarchy_paths_for_check,
        compact_hierarchy_evidence,
    )
    from hierwalk.connect.shared.request import ConnectivityCheck

    chk = ConnectivityCheck("top.a.b.sig", "top.a.b.sig", check_id="c1")
    pref = _preferred_hierarchy_paths_for_check(chk)
    evidence = [
        HierarchyEvidenceRow("c1", "a", "inst", "top", "hit", "TOP"),
        HierarchyEvidenceRow("c1", "a", "inst", "top.a", "hit", "A"),
        HierarchyEvidenceRow("c1", "a", "inst", "top.a.b", "hit", "B"),
        HierarchyEvidenceRow("c1", "a", "wire", "top.a.b.sig", "hit", "B"),
        HierarchyEvidenceRow("c1", "b", "inst", "top", "hit", "TOP"),
        HierarchyEvidenceRow("c1", "b", "inst", "top.a", "hit", "A"),
        HierarchyEvidenceRow("c1", "b", "inst", "top.a.b", "hit", "B"),
        HierarchyEvidenceRow("c1", "b", "wire", "top.a.b.sig", "hit", "B"),
    ]
    compact = compact_hierarchy_evidence(evidence, preferred=pref)
    assert len(compact) == 2
    by_side = {row.side: row for row in compact}
    assert by_side["a"].path == "top.a.b.sig"
    assert by_side["b"].path == "top.a.b.sig"


def test_incremental_hierarchy_writer_one_final_row_per_side(tmp_path: Path):
    from hierwalk.connect.pipeline.artifacts import IncrementalHierarchyTsvWriter
    from hierwalk.connect.shared.request import ConnectivityCheck
    from hierwalk.path_walk import run_path_walk_connect
    from hierwalk.filelist import parse_filelist

    rtl = tmp_path / "top.v"
    rtl.write_text(
        "module B; wire sig; endmodule\n"
        "module A; B b (); endmodule\n"
        "module top; A a (); endmodule\n",
        encoding="utf-8",
    )
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{rtl.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    out_dir = tmp_path / ".db_top"
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.a.b.sig", "top.a.b.sig", check_id="c1"),),
        top="top",
    )
    run_path_walk_connect(
        req,
        fl,
        top="top",
        no_cache=True,
        connect_phase="text",
        connect_output_dir=out_dir,
    )
    hier_text = out_dir / "hierarchy.text.tsv"
    assert hier_text.is_file()
    body = hier_text.read_text(encoding="utf-8")
    a_rows = [ln for ln in body.splitlines() if ln.startswith("c1\ta\t")]
    b_rows = [ln for ln in body.splitlines() if ln.startswith("c1\tb\t")]
    assert len(a_rows) == 1 and "top.a.b.sig" in a_rows[0]
    assert len(b_rows) == 1 and "top.a.b.sig" in b_rows[0]


def test_compact_hierarchy_evidence_drops_redundant_inst_hits():
    from hierwalk.connect.pipeline.artifacts import (
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
    assert len(compact) == 1
    assert compact[0].path == "top.a.b.sig"
    assert compact[0].kind == "wire"


def test_format_connect_hierarchy_tsv_includes_absolute_rtl_path(tmp_path: Path):
    rtl = tmp_path / "leaf.v"
    rtl.write_text("module leaf(); endmodule\n", encoding="utf-8")
    rtl_abs = str(rtl.resolve())
    rows_by_path = {
        "top": FlatRow(
            full_path="top",
            inst_leaf="top",
            module="TOP",
            depth=0,
            parent_path=None,
            file=str(tmp_path / "top.v"),
        ),
        "top.u_leaf": FlatRow(
            full_path="top.u_leaf",
            inst_leaf="u_leaf",
            module="leaf",
            depth=1,
            parent_path="top",
            file=rtl_abs,
        ),
    }
    result = ConnectResult(
        check_id="rtl",
        endpoint_a=ConnectEndpoint("top.u_leaf.sig", "top.u_leaf", "sig", "leaf"),
        endpoint_b=ConnectEndpoint("top.clk", "top", "clk", "TOP", port_found=True),
        connected=False,
        mode="port-port",
    )
    tsv = format_connect_hierarchy_tsv([result], rows_by_path, phase="text")
    parsed = _tsv_rows(tsv)
    assert "rtl" in parsed[0]
    sig_rows = [row for row in parsed if row["path"] == "top.u_leaf.sig"]
    assert len(sig_rows) == 1 and sig_rows[0]["rtl"] == rtl_abs


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
    assert len(a_rows) == 1
    assert a_rows[0]["path"] == "top.missing.sig"
    assert a_rows[0]["status"] == "miss"
    b_rows = [row for row in parsed if row["side"] == "b"]
    assert len(b_rows) == 1
    assert b_rows[0]["path"] == "top.clk"
    assert b_rows[0]["status"] == "hit"


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
    assert lines[0].startswith("# connect results")
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


def test_format_connect_results_tsv_phase_column_hints():
    result = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=True,
        mode="port-port",
        connected_text=True,
    )
    text_tsv = format_connect_results_tsv([result], phase="text")
    assert "connected_text=text bloom" in text_tsv
    assert "hop tags:" in text_tsv
    logical_tsv = format_connect_results_tsv(
        [ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            connected_logical=False,
        )],
        phase="logical",
    )
    assert "connected_text=prior text pass" in logical_tsv


def test_reorder_connect_checks_by_b_endpoint():
    from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest

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
    from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest

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


def test_merge_refined_connect_results_endpoint_fallback():
    orig = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=True,
        mode="port-port",
        connected_text=True,
        check_id="",
    )
    refined = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=False,
        mode="port-port",
        errors=["logical miss"],
        check_id="",
    )
    merge_refined_connect_results([orig], [refined])
    assert orig.connected is False
    assert orig.connected_text is True
    assert orig.errors == ["logical miss"]


def test_build_logical_connect_request_empty_list_no_gate():
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.a", "top.b", check_id="a"),
            ConnectivityCheck("top.x", "top.y", check_id="x"),
        ),
        top="top",
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, [])
    assert run_n == 2
    assert skip_n == 0
    assert [c.check_id for c in logical_req.checks] == ["a", "x"]


def test_apply_text_verdicts_endpoint_fallback_without_check_id():
    shell = [
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=False,
            mode="unknown",
            check_id="req-id",
        ),
    ]
    text_rows = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            check_id="",
        ),
    )
    apply_text_verdicts_to_results(shell, text_rows)
    assert shell[0].connected_text is True
    assert shell[0].connected is True


def test_apply_logical_coi_failure_preserves_connected_text():
    leaf = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=True,
        mode="port-port",
        connected_text=True,
        check_id="hit",
    )
    apply_logical_coi_failure_to_results(
        [leaf],
        [ConnectivityCheck("top.a", "top.b", check_id="hit")],
        "connect-coi failed: RuntimeError('boom')",
    )
    assert leaf.connected_text is True
    assert leaf.connected is False
    assert leaf.connected_logical is False
    assert "connect-coi failed" in leaf.errors[0]


def test_build_logical_connect_request_duplicate_endpoint_conservative():
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.a", "top.b", check_id="x"),),
        top="top",
    )
    text_results = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            check_id="",
        ),
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=False,
            mode="port-port",
            connected_text=False,
            check_id="",
        ),
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, text_results)
    assert run_n == 0
    assert skip_n == 1
    assert logical_req.checks == ()


def test_build_logical_connect_request_endpoint_fallback_without_check_id():
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.a", "top.b", check_id="req-id"),),
        top="top",
    )
    text_results = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            check_id="",
        ),
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, text_results)
    assert run_n == 1
    assert skip_n == 0
    assert logical_req.checks[0].endpoint_a == "top.a"
    assert logical_req.checks[0].endpoint_b == "top.b"


def test_build_logical_connect_request_skips_text_misses():
    req = ConnectivityRequest(
        checks=(
            ConnectivityCheck("top.a", "top.b", check_id="hit"),
            ConnectivityCheck("top.x", "top.y", check_id="miss"),
        ),
        top="top",
    )
    text_results = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            check_id="hit",
        ),
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
            endpoint_b=ConnectEndpoint("top.y", "top", "y", "top"),
            connected=False,
            mode="port-port",
            connected_text=False,
            check_id="miss",
        ),
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, text_results)
    assert run_n == 1
    assert skip_n == 1
    assert [c.check_id for c in logical_req.checks] == ["hit"]


def test_load_text_connect_results_from_tsv(tmp_path: Path):
    out = tmp_path / "conn.text.tsv"
    write_connect_phase_tsv(
        out,
        [
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
                endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
                connected=True,
                mode="port-port",
                connected_text=True,
                check_id="hit",
            ),
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
                endpoint_b=ConnectEndpoint("top.y", "top", "y", "top"),
                connected=False,
                mode="port-port",
                connected_text=False,
                check_id="miss",
            ),
        ],
        phase="text",
    )
    loaded = load_text_connect_results_from_tsv(out)
    assert len(loaded) == 2
    by_id = {r.check_id: r.connected_text for r in loaded}
    assert by_id["hit"] is True
    assert by_id["miss"] is False


def test_build_logical_connect_request_from_tsv_expand_leaves():
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("[top.a, top.x]", "top.b", check_id="fan"),),
        top="top",
    )
    tsv_rows = (
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=True,
            mode="port-port",
            connected_text=True,
            check_id="fan->0",
        ),
        ConnectResult(
            endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
            endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
            connected=False,
            mode="port-port",
            connected_text=False,
            check_id="fan->1",
        ),
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, tsv_rows)
    assert run_n == 1
    assert skip_n == 1
    assert logical_req.checks[0].check_id == "fan->0"


def test_build_logical_connect_request_expand_partial_hits():
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("[top.a, top.x]", "top.b", check_id="fan"),),
        top="top",
    )
    parent = ConnectResult(
        endpoint_a=ConnectEndpoint("[top.a, top.x]", "top", "", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=False,
        mode="expanded",
        connected_text=False,
        check_id="fan",
        sub_results=(
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
                endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
                connected=True,
                mode="port-port",
                connected_text=True,
                check_id="fan->0",
            ),
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
                endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
                connected=False,
                mode="port-port",
                connected_text=False,
                check_id="fan->1",
            ),
        ),
    )
    logical_req, run_n, skip_n = build_logical_connect_request(req, [parent])
    assert run_n == 1
    assert skip_n == 1
    assert logical_req.checks[0].check_id == "fan->0"


def test_clear_logical_cache_preserves_text_grep_cache(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    index = DesignIndex.build({str(rtl): rtl.read_text(encoding="utf-8")})
    _, rows = elaborate(index, "top")
    session = ConnectivitySession(rows=rows, index=index, top="top")
    session.text_grep_cache[("k",)] = object()  # type: ignore[index]
    session.mod_cache[("m",)] = object()  # type: ignore[index]
    session.clear_logical_cache()
    assert session.text_grep_cache
    assert not session.mod_cache


def test_merge_refined_into_expand_sub_results():
    parent = ConnectResult(
        endpoint_a=ConnectEndpoint("[top.a, top.x]", "top", "", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=False,
        mode="expanded",
        connected_text=False,
        check_id="fan",
        sub_results=(
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
                endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
                connected=True,
                mode="port-port",
                connected_text=True,
                check_id="fan->0",
            ),
            ConnectResult(
                endpoint_a=ConnectEndpoint("top.x", "top", "x", "top"),
                endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
                connected=False,
                mode="port-port",
                connected_text=False,
                check_id="fan->1",
            ),
        ),
    )
    refined = ConnectResult(
        endpoint_a=ConnectEndpoint("top.a", "top", "a", "top"),
        endpoint_b=ConnectEndpoint("top.b", "top", "b", "top"),
        connected=True,
        mode="port-port",
        check_id="fan->0",
    )
    merge_refined_connect_results([parent], [refined])
    assert parent.sub_results[0].connected is True
    assert parent.sub_results[0].connected_text is True
    assert parent.sub_results[1].connected is False


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
    from hierwalk.connect.pipeline.artifacts import default_verification_artifact_name

    assert default_verification_artifact_name("run_conn_check") == "conn.tsv"
    assert default_verification_artifact_name("run_io_trace") == "io_trace.tsv"
    assert default_verification_artifact_name("run_cone_trace") == "cone_trace.tsv"
    assert default_verification_artifact_name("run_on_full_index") == "instances.tsv"


def test_verification_output_path_text_suffix():
    assert verification_output_path("trace.tsv", "text").name == "trace.text.tsv"
    assert verification_output_path("trace.tsv", "logical").name == "trace.tsv"