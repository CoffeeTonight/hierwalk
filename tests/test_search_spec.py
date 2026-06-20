"""Structured search spec parsing and execution."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.preprocess import preprocess_file
from hierwalk.run_request import parse_run_request_json
from hierwalk.search_spec import (
    build_search_spec_from_legacy,
    document_has_search,
    execute_search_spec,
    parse_search_spec_block,
    resolve_search_spec,
)


def _build_rows(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module chip_top;
  cpu_blk u_CPU0 ( );
  gpu_blk u_gpu0 ( );
endmodule
module cpu_blk;
  leaf u_leaf_cpu ( );
endmodule
module gpu_blk;
  leaf u_leaf_gpu ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "chip_top")
    return index, rows


def test_parse_search_spec_block():
    spec = parse_search_spec_block(
        {
            "instance": ["u_*cpu*", "*gpu*"],
            "path": ["*.*cpu*"],
            "hierarchy_path": ["chip_top.u_*.*cpu*"],
            "case_insensitive": True,
            "search_module": True,
        }
    )
    assert spec.instance == ("u_*cpu*", "*gpu*")
    assert spec.path == ("*.*cpu*",)
    assert spec.hierarchy_path == ("chip_top.u_*.*cpu*",)
    assert spec.case_insensitive
    assert spec.search_module


def test_resolve_search_spec_structured():
    data = {
        "search": {
            "instance": "u_*cpu*",
            "case_insensitive": True,
        },
        "search_path": "chip_top.u_*",
    }
    spec = resolve_search_spec(data)
    assert spec is not None
    assert spec.instance == ("u_*cpu*",)
    assert spec.case_insensitive
    assert spec.hierarchy_path == ("chip_top.u_*",)


def test_resolve_search_spec_legacy():
    spec = resolve_search_spec(
        {
            "search": "u_*cpu*,*.*cpu*",
            "search_path": "chip_top.u_*",
            "search_case_insensitive": True,
        }
    )
    assert spec is not None
    assert spec.instance == ("u_*cpu*",)
    assert spec.path == ("*.*cpu*",)
    assert spec.hierarchy_path == ("chip_top.u_*",)
    assert spec.case_insensitive


def test_document_has_search_structured():
    assert document_has_search({"search": {"instance": "u_*"}})
    assert not document_has_search({"search": {}})


def test_parse_run_request_json_structured_search(tmp_path):
    cfg = parse_run_request_json(
        {
            "filelist": "design.f",
            "mode": "search",
            "search": {
                "instance": ["u_*cpu*"],
                "case_insensitive": True,
            },
        },
        base_dir=tmp_path,
    )
    assert cfg.mode == "search"
    assert cfg.search is None
    assert cfg.search_spec is not None
    assert cfg.search_spec.instance == ("u_*cpu*",)
    assert cfg.search_case_insensitive


def test_execute_search_spec_pattern_kinds(tmp_path):
    index, rows = _build_rows(tmp_path)

    inst_spec = parse_search_spec_block({"instance": ["u_CPU0"]})
    inst_hits = execute_search_spec(rows, index, inst_spec)
    assert {h.full_path for h in inst_hits} == {"chip_top.u_CPU0"}

    path_spec = parse_search_spec_block({"path": ["*u_leaf*"]})
    path_hits = execute_search_spec(rows, index, path_spec)
    assert {h.full_path for h in path_hits} == {
        "chip_top.u_CPU0.u_leaf_cpu",
        "chip_top.u_gpu0.u_leaf_gpu",
    }

    hier_spec = parse_search_spec_block({"hierarchy_path": ["chip_top.u_*"]})
    hier_hits = execute_search_spec(rows, index, hier_spec)
    assert {h.full_path for h in hier_hits} == {
        "chip_top.u_CPU0",
        "chip_top.u_gpu0",
    }


def test_case_insensitive_instance_match(tmp_path):
    index, rows = _build_rows(tmp_path)
    spec = parse_search_spec_block(
        {"instance": ["u_*cpu0"], "case_insensitive": True}
    )
    hits = execute_search_spec(rows, index, spec)
    assert {h.full_path for h in hits} == {"chip_top.u_CPU0"}

    sensitive = parse_search_spec_block(
        {"instance": ["u_*cpu0"], "case_insensitive": False}
    )
    assert execute_search_spec(rows, index, sensitive) == []


def test_legacy_build_search_spec_dot_routing():
    spec = build_search_spec_from_legacy(search="u_leaf,*.*cpu*")
    assert spec is not None
    assert spec.instance == ("u_leaf",)
    assert spec.path == ("*.*cpu*",)