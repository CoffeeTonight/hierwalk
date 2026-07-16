"""Grep-first hierarchy resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.inst_scan import coarse_hierarchy_path
from hierwalk.hierarchy_grep import (
    GREP_HIE_JSON_NAME,
    HierarchyGrepSession,
    _HgrepBuildHeartbeat,
    _HgrepBuildProgress,
    build_file_grep_index,
    build_module_index,
    dump_file_grep_index,
    dump_grep_hie,
    format_hierarchy_grep_report,
    grep_hie_sources_match,
    grep_modules_in_file,
    hierarchy_grep_report,
    load_file_grep_index,
    load_grep_hie,
    remove_grep_hie,
    resolve_grep_hie_path,
    resolve_hierarchy_grep,
)


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_grep_modules_and_duplicate_index(tmp_path: Path):
    a = _write(
        tmp_path,
        "a.v",
        "module dup (); endmodule\nmodule only_a (); endmodule\n",
    )
    b = _write(tmp_path, "b.v", "module dup (); endmodule\n")
    assert grep_modules_in_file(a) == ["dup", "only_a"]
    index = build_module_index([a, b])
    assert index["dup"] == [a, b]
    assert index["only_a"] == [a]


def test_resolve_inst_chain_and_leaf_signal(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (input clk);
          wire inner;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_a.inner", top="top", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result["nodes"][-1]["kind"] == "signal"
    assert result["nodes"][-1]["file"] == top_v
    assert result["nodes"][1]["child_module"] == "child"


def test_last_segment_can_be_inst(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf (); endmodule
        module top;
          leaf u_b ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_b", top="top", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result["nodes"][-1]["kind"] == "inst"
    assert result["nodes"][-1]["child_module"] == "leaf"


def test_intermediate_hop_db_first_infer_and_index(tmp_path: Path):
    """``\\bu_a\\b`` in body + ``child`` in module_index → inst hop without cell parse."""
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          `ifdef CHIP_HAS_CHILD
          child u_a ();
          `endif
        endmodule
        """,
    )
    index = build_module_index([top_v])
    result = resolve_hierarchy_grep(
        "top.u_a.out", top="top", rtl_paths=[top_v], module_index=index
    )
    assert result["ok"] is True
    assert result["nodes"][1]["child_module"] == "child"


def test_intermediate_hop_db_first_fails_when_module_not_in_index(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
          ghost_module u_ghost ();
        endmodule
        """,
    )
    index = build_module_index([top_v])
    result = resolve_hierarchy_grep(
        "top.u_ghost.out", top="top", rtl_paths=[top_v], module_index=index
    )
    assert result["ok"] is False
    assert "u_ghost" in result.get("error", "")


def test_ifdef_multi_branch_prunes_by_downstream_leaf(tmp_path: Path):
    """Same inst under ifdef/elsif/else → fan-out; leaf prunes dead branches."""
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module ModA (output logic o);
          assign o = 1'b0;
        endmodule
        module ModB (output logic only_b);
          assign only_b = 1'b1;
        endmodule
        module ModC (output logic o);
          assign o = 1'b1;
        endmodule
        module top;
          `ifdef FEAT_A
          ModA
          u_foo ();
          `elsif FEAT_B
          ModB
          u_foo ();
          `else
          ModC
          u_foo ();
          `endif
        endmodule
        """,
    )
    index = build_module_index([top_v])
    unique = resolve_hierarchy_grep(
        "top.u_foo.only_b", top="top", rtl_paths=[top_v], module_index=index
    )
    assert unique["ok"] is True
    assert unique["ambiguous"] is False
    assert unique["nodes"][1]["child_module"] == "ModB"

    shared = resolve_hierarchy_grep(
        "top.u_foo.o", top="top", rtl_paths=[top_v], module_index=index
    )
    assert shared["ok"] is True
    assert shared["ambiguous"] is True
    assert len(shared.get("candidates", ())) >= 2
    child_mods = {
        c["nodes"][1]["child_module"] for c in shared["candidates"] if c.get("nodes")
    }
    assert child_mods >= {"ModA", "ModC"}


def test_intermediate_hop_db_first_requires_word_boundary(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (); endmodule
        module top;
          child u_ab ();
        endmodule
        """,
    )
    index = build_module_index([top_v])
    result = resolve_hierarchy_grep(
        "top.u_a.out", top="top", rtl_paths=[top_v], module_index=index
    )
    assert result["ok"] is False


def test_wire_inst_collision_prefers_inst_on_leaf(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "scope_b.v",
        """
        module child (); endmodule
        module scope_b;
          wire u_b;
          child u_b ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("scope_b.u_b", top="scope_b", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result["nodes"][-1]["kind"] == "inst"
    assert result["nodes"][-1]["child_module"] == "child"


def test_resolve_body_output_ports_on_separate_lines(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
          output a_0;
          output a_1;
        endmodule
        """,
    )
    for leaf in ("a_0", "a_1"):
        result = resolve_hierarchy_grep(f"top.{leaf}", top="top", rtl_paths=[top_v])
        assert result["ok"] is True, (leaf, result.get("error"), result.get("nodes"))
        assert result["nodes"][-1]["kind"] in ("port", "signal")


def test_resolve_body_output_ports_comma_list(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
          output a_0, a_1;
        endmodule
        """,
    )
    for leaf in ("a_0", "a_1"):
        result = resolve_hierarchy_grep(f"top.{leaf}", top="top", rtl_paths=[top_v])
        assert result["ok"] is True, (leaf, result.get("error"))


def test_internal_segment_must_be_inst(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module top;
          wire u_a;
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_a.sig", top="top", rtl_paths=[top_v])
    assert result["ok"] is False
    assert "instance" in result["error"]


def test_resolve_includes_timing_fields(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (); endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_a", top="top", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result.get("started_at")
    assert result.get("resolved_at")
    assert result.get("total_elapsed_ms", 0) >= 0
    assert all("elapsed_ms" in node for node in result["nodes"])


def test_report_contains_json_block(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        "module top; wire x; endmodule\n",
    )
    report, data = hierarchy_grep_report("top.x", top="top", rtl_paths=[top_v])
    assert "json:" in report
    assert '"ok": true' in report
    assert data["nodes"][-1]["found"] is True


def test_coarse_hierarchy_path_strips_array_indices():
    assert coarse_hierarchy_path("top.u_arr[0].sig[3]") == "top.u_arr.sig"
    assert coarse_hierarchy_path("top.u_matrix.outer[0].inner[0].u_nest") == (
        "top.u_matrix.outer.inner.u_nest"
    )


def test_resolve_strips_array_indices_like_text_conn(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module leaf (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          leaf u_arr [0:1] ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_arr[0]", top="top", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result["hierarchy_input"] == "top.u_arr[0]"
    assert result["hierarchy"] == "top.u_arr"
    assert result["nodes"][-1]["child_module"] == "leaf"


def test_resolve_strips_port_slice_on_leaf(tmp_path: Path):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic [3:0] nibble);
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    result = resolve_hierarchy_grep("top.u_a.nibble[2]", top="top", rtl_paths=[top_v])
    assert result["ok"] is True
    assert result["hierarchy"] == "top.u_a.nibble"
    assert result["nodes"][-1]["kind"] == "port"


def test_resolve_uses_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    monkeypatch.chdir(tmp_path)
    result = resolve_hierarchy_grep("top.x", top="top", rtl_paths=[Path("top.v")])
    assert result["ok"] is True
    for node in result["nodes"]:
        assert Path(node["file"]).is_absolute()
        if node.get("hit_file"):
            assert Path(node["hit_file"]).is_absolute()
    assert result["nodes"][0]["file"] == top_v


def test_build_file_grep_index_inverts_module_index(tmp_path: Path):
    a = _write(tmp_path, "a.v", "module dup (); endmodule\nmodule only_a (); endmodule\n")
    b = _write(tmp_path, "b.v", "module dup (); endmodule\n")
    module_index = build_module_index([a, b])
    file_index = build_file_grep_index(module_index)
    assert set(file_index) == {a, b}
    assert file_index[a]["modules"] == ["dup", "only_a"]
    assert file_index[b]["modules"] == ["dup"]
    assert file_index[a]["file"] == a
    assert file_index[a]["module_count"] == 2


def _consume_file_index(file_index: dict[str, dict]) -> list[str]:
    return sorted(path for path, info in file_index.items() if info.get("modules"))


def test_dump_and_load_file_grep_index_roundtrip(tmp_path: Path):
    a = _write(tmp_path, "a.v", "module top (); endmodule\n")
    module_index = build_module_index([a])
    file_index = build_file_grep_index(module_index)
    out = dump_file_grep_index(file_index, tmp_path / "grep" / "file_grep_index.json")
    assert Path(out).is_absolute()
    loaded = load_file_grep_index(out)
    assert _consume_file_index(loaded) == _consume_file_index(file_index)


def test_session_can_write_file_and_pass_data_to_function(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    json_path = tmp_path / "file_grep_index.json"
    session = HierarchyGrepSession.from_rtl_paths(
        [top_v],
        file_grep_index_path=json_path,
    )
    result, file_index = session.resolve_with_file_index("top.x", top="top")
    assert result["ok"] is True
    assert _consume_file_index(file_index) == [top_v]
    assert session.file_grep_index_ready() is True
    assert Path(session.file_grep_index_path).is_file()
    assert _consume_file_index(load_file_grep_index(session.file_grep_index_path)) == [top_v]


def test_session_builds_file_grep_index_in_background(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    session = HierarchyGrepSession.from_rtl_paths([top_v])
    result = session.resolve("top.x", top="top")
    assert result["ok"] is True
    file_index = session.file_grep_index(wait=True)
    assert top_v in file_index
    assert file_index[top_v]["modules"] == ["top"]
    assert all(Path(path).is_absolute() for path in file_index)


def test_missing_top_in_index(tmp_path: Path):
    empty = _write(tmp_path, "empty.v", "module other (); endmodule\n")
    result = resolve_hierarchy_grep("top.u", top="top", rtl_paths=[empty])
    assert result["ok"] is False
    assert "not in grep index" in result["error"]


def test_dump_and_load_grep_hie_roundtrip(tmp_path: Path):
    top_v = _write(tmp_path, "top.v", "module top; wire x; endmodule\n")
    session = HierarchyGrepSession.from_rtl_paths([top_v], build_file_index_background=False)
    cache_path = tmp_path / "work" / GREP_HIE_JSON_NAME
    dump_grep_hie(session, cache_path, top="top")
    loaded = load_grep_hie(cache_path)
    assert loaded["top"] == "top"
    assert loaded["rtl_paths"] == [top_v]
    assert loaded["module_index"]["top"] == [top_v]
    restored = HierarchyGrepSession.from_grep_hie_cache(loaded, cache_path=cache_path)
    assert restored.resolve("top.x", top="top")["ok"] is True


def test_grep_hie_sources_match_requires_exact_set(tmp_path: Path):
    a = _write(tmp_path, "a.v", "module top (); endmodule\n")
    b = _write(tmp_path, "b.v", "module other (); endmodule\n")
    session = HierarchyGrepSession.from_rtl_paths([a], build_file_index_background=False)
    cache_path = resolve_grep_hie_path(tmp_path)
    dump_grep_hie(session, cache_path, top="top")
    cached = load_grep_hie(cache_path)
    assert grep_hie_sources_match(cached, [a])
    assert not grep_hie_sources_match(cached, [a, b])
    assert remove_grep_hie(cache_path)
    assert not cache_path.is_file()


def test_session_resolve_reuses_module_body_cache(tmp_path: Path, monkeypatch):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
          child u_b ();
        endmodule
        """,
    )
    import hierwalk.hierarchy_grep as hg

    read_count = 0
    orig_read = hg._read_text

    def counting_read(path):
        nonlocal read_count
        read_count += 1
        return orig_read(path)

    monkeypatch.setattr(hg, "_read_text", counting_read)

    session = HierarchyGrepSession.from_rtl_paths(
        [top_v],
        build_file_index_background=False,
    )
    session.resolve("top.u_a.out", top="top")
    after_first = read_count
    assert after_first == 1

    session.resolve("top.u_b.out", top="top")
    after_second = read_count
    assert after_second == 1

    session.resolve("top.u_a.out", top="top")
    assert read_count == after_second

    assert len(session._module_body_cache) >= 1


def test_resolve_without_session_still_uses_per_call_cache(tmp_path: Path, monkeypatch):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
        endmodule
        """,
    )
    import hierwalk.hierarchy_grep as hg

    read_count = 0
    orig_read = hg._read_text

    def counting_read(path):
        nonlocal read_count
        read_count += 1
        return orig_read(path)

    monkeypatch.setattr(hg, "_read_text", counting_read)

    index = build_module_index([top_v])
    resolve_hierarchy_grep(
        "top.u_a.out",
        top="top",
        rtl_paths=[top_v],
        module_index=index,
    )
    first_reads = read_count
    resolve_hierarchy_grep(
        "top.u_a.out",
        top="top",
        rtl_paths=[top_v],
        module_index=index,
    )
    assert read_count == first_reads * 2


def test_hgrep_build_heartbeat_emits_current_file(tmp_path: Path, monkeypatch):
    paths = [
        _write(tmp_path, f"m{i}.v", f"module m{i} (); endmodule\n")
        for i in range(3)
    ]
    logs: list[str] = []

    def slow_grep(path):
        import time

        time.sleep(0.05)
        return [f"m{Path(path).stem}"]

    monkeypatch.setattr(
        "hierwalk.hierarchy_grep.grep_modules_in_file",
        slow_grep,
    )
    progress = _HgrepBuildProgress(len(paths))
    with _HgrepBuildHeartbeat(progress, on_emit=logs.append, interval_sec=0.05):
        build_module_index(paths, progress=progress)

    assert any("hgrep-hie heartbeat" in line for line in logs)
    assert any("files_done=" in line and "folder:" in line for line in logs)