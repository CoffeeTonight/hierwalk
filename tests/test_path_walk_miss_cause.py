"""Path-walk miss lines include short ``cause=`` tags."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.hierarchy_log import (
    _ANSI_RED,
    _ANSI_RESET,
    classify_path_walk_inst_miss,
    colorize_path_walk_miss_reason,
    path_walk_inst_miss_reason,
)
from hierwalk.models import InstanceEdge, ModuleRecord
from hierwalk.path_walk import build_path_walk_state_from_specs


def test_classify_dup_module_when_multiple_rtl_candidates():
    rec = ModuleRecord(module_name="B", file_path="/rtl/b1.v")
    edges = [InstanceEdge("x", "X")]
    cause = classify_path_walk_inst_miss(
        parent_rec=rec,
        miss_leaf="c",
        edges=edges,
        candidate_files=["/rtl/b1.v", "/rtl/b2.v"],
    )
    assert cause == "dup-module"


def test_classify_ifdef_filtered_when_raw_source_has_inst_name():
    rec = ModuleRecord(module_name="top", file_path="/rtl/top.v")
    cause = classify_path_walk_inst_miss(
        parent_rec=rec,
        miss_leaf="u_cpusystem_top",
        edges=[],
        candidate_files=["/rtl/top.v"],
        raw_source_has_inst=True,
    )
    assert cause == "ifdef-filtered"


def test_classify_no_inst_when_single_file_and_edges_present():
    rec = ModuleRecord(module_name="top", file_path="/rtl/top.v")
    edges = [InstanceEdge("u_a", "A")]
    cause = classify_path_walk_inst_miss(
        parent_rec=rec,
        miss_leaf="u_missing",
        edges=edges,
        candidate_files=["/rtl/top.v"],
    )
    assert cause == "no-inst"


def test_trace_miss_includes_cause_tag(tmp_path: Path):
    top_v = tmp_path / "top.v"
    top_v.write_text("module top; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(f"{top_v.resolve()}\n", encoding="utf-8")
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
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
    assert "cause=no-inst" in text
    assert "miss inst=u_missing under top" in text


def test_classify_array_index_when_bare_name_used():
    rec = ModuleRecord(module_name="B", file_path="/rtl/b.v")
    edges = [InstanceEdge("c[0:1][0:1]", "md2d_c")]
    cause = classify_path_walk_inst_miss(
        parent_rec=rec,
        miss_leaf="c",
        edges=edges,
        candidate_files=["/rtl/b.v"],
    )
    assert cause == "array-index"


def test_classify_ignored_when_parent_stop_reason():
    rec = ModuleRecord(
        module_name="BB",
        file_path="/rtl/bb.v",
        stop_reason="ignorePath",
    )
    cause = classify_path_walk_inst_miss(
        parent_rec=rec,
        miss_leaf="u",
        edges=[],
        candidate_files=["/rtl/bb.v"],
    )
    assert cause == "ignored"


def test_colorize_miss_reason_wraps_cause_summary_only():
    plain = (
        "miss inst=c under top.a.b (cause=no-inst; instance edge not found in parent module; "
        "have: x->X; pw-db B files: b1.v)  parent module=B rtl=b1.v"
    )
    colored = colorize_path_walk_miss_reason(plain, enable=True)
    assert colored.startswith("miss inst=c under top.a.b (")
    assert (
        f"{_ANSI_RED}cause=no-inst; instance edge not found in parent module{_ANSI_RESET}"
        in colored
    )
    assert "; have: x->X" in colored
    assert _ANSI_RED not in colored.split(_ANSI_RESET, 1)[-1].split("; have:")[0]


def test_colorize_miss_reason_skips_hint_parentheses():
    plain = path_walk_inst_miss_reason(
        parent_mod="BLK",
        parent_rec=ModuleRecord(module_name="BLK", file_path="/rtl/blk.v"),
        miss_leaf="CORE",
        edges=[InstanceEdge("u_core", "CORE")],
        candidate_files=["/rtl/blk.v"],
    )
    line = (
        f"miss inst=CORE under top.blk ({plain})  parent module=BLK rtl=blk.v"
    )
    colored = colorize_path_walk_miss_reason(line, enable=True)
    assert f"{_ANSI_RED}cause=type-not-inst" in colored
    assert "use inst name" in colored
    assert colored.count(_ANSI_RED) == 1


def test_path_walk_inst_miss_reason_includes_type_hint():
    edges = [InstanceEdge("u_core", "CORE")]
    reason = path_walk_inst_miss_reason(
        parent_mod="BLK",
        parent_rec=ModuleRecord(module_name="BLK", file_path="/rtl/blk.v"),
        miss_leaf="CORE",
        edges=edges,
        candidate_files=["/rtl/blk.v"],
    )
    assert "cause=type-not-inst" in reason
    assert "use inst name 'u_core'" in reason