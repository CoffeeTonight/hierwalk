"""Hierarchy coverage audit: untouched RTL, filelists, directory roots."""

from __future__ import annotations

from pathlib import Path

from hierwalk.coverage_audit import (
    compute_coverage_audit,
    minimal_unused_dir_roots,
)
from hierwalk.elab import elaborate
from hierwalk.filelist import filelist_provenance_maps, parse_filelist
from hierwalk.index import DesignIndex


def test_minimal_unused_dir_roots_collapses_siblings(tmp_path: Path):
    used = [str(tmp_path / "rtl" / "core" / "cpu.v")]
    unused = [
        str(tmp_path / "rtl" / "pcie" / "link.v"),
        str(tmp_path / "rtl" / "pcie" / "phy.v"),
    ]
    roots = minimal_unused_dir_roots(unused, used)
    assert roots == [str((tmp_path / "rtl" / "pcie").resolve())]


def test_minimal_unused_dir_roots_skips_shared_parent(tmp_path: Path):
    used = [str(tmp_path / "proj" / "rtl" / "core" / "top.v")]
    unused = [str(tmp_path / "proj" / "rtl" / "dma" / "ch.v")]
    roots = minimal_unused_dir_roots(unused, used)
    assert roots == [str((tmp_path / "proj" / "rtl" / "dma").resolve())]


def test_minimal_unused_dir_roots_entire_branch(tmp_path: Path):
    used = [str(tmp_path / "proj" / "rtl" / "core" / "cpu.v")]
    unused = [str(tmp_path / "proj" / "vendor" / "ip" / "stub.v")]
    roots = minimal_unused_dir_roots(unused, used)
    assert roots == [str((tmp_path / "proj" / "vendor").resolve())]


def test_coverage_audit_unused_filelists_and_dirs(tmp_path: Path):
    core_dir = tmp_path / "rtl" / "core"
    pcie_dir = tmp_path / "rtl" / "pcie"
    core_dir.mkdir(parents=True)
    pcie_dir.mkdir(parents=True)

    core_v = core_dir / "core.v"
    pcie_v = pcie_dir / "pcie.v"
    top_v = tmp_path / "top.v"
    core_v.write_text(
        "module core(input clk); endmodule\n",
        encoding="utf-8",
    )
    pcie_v.write_text(
        "module pcie(input clk); endmodule\n",
        encoding="utf-8",
    )
    top_v.write_text(
        "module top(input clk);\n  core u_core(.clk(clk));\nendmodule\n",
        encoding="utf-8",
    )

    core_f = tmp_path / "core.f"
    pcie_f = tmp_path / "pcie.f"
    top_f = tmp_path / "top.f"
    core_f.write_text("rtl/core/core.v\n", encoding="utf-8")
    pcie_f.write_text("rtl/pcie/pcie.v\n", encoding="utf-8")
    top_f.write_text(
        f"-f {core_f.name}\n-f {pcie_f.name}\n{top_v.name}\n",
        encoding="utf-8",
    )

    fl = parse_filelist(str(top_f), index_cwd=str(tmp_path))
    via_map, chain_map = filelist_provenance_maps(fl)
    index = DesignIndex.build_from_sources(
        [str(p.resolve()) for p in fl.source_files],
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=fl.defines,
        jobs=1,
        file_via_filelist=via_map,
        file_filelist_chain=chain_map,
        filelist_info=fl.filelist_info,
        filelist_children=fl.filelist_children,
        filelist_edges=fl.filelist_edges,
    )
    _, rows = elaborate(index, "top")
    audit = compute_coverage_audit(index, fl, rows, tops=["top"])

    assert audit.listed_rtl == 3
    assert audit.elaborated_rtl == 2
    assert audit.untouched_rtl == 1
    assert str(pcie_f.resolve()) in audit.unused_filelists
    assert str(core_f.resolve()) not in audit.unused_filelists
    assert list(audit.untouched_dir_roots) == [str(pcie_dir.resolve())]