"""ignore-filelist: skip RTL by listing .f provenance."""

from __future__ import annotations

from pathlib import Path

from hierwalk.cache import build_design_index
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.ignore_path import filelist_path_matches, partition_sources


def test_filelist_path_matches_basename():
    listing = "/proj/lists/pcie_block.f"
    assert filelist_path_matches(listing, patterns=["pcie_block.f"])
    assert filelist_path_matches(
        listing,
        chain="/proj/top.f -> /proj/lists/pcie_block.f",
        patterns=["pcie_block.f"],
    )
    assert not filelist_path_matches(listing, patterns=["soc_block.f"])


def test_partition_sources_by_listing_filelist(tmp_path: Path):
    pcie_f = tmp_path / "pcie_block.f"
    soc_f = tmp_path / "soc.f"
    pcie_dir = tmp_path / "pcie_rtl"
    soc_dir = tmp_path / "soc_rtl"
    pcie_dir.mkdir()
    soc_dir.mkdir()
    pcie_v = pcie_dir / "pcie.v"
    soc_v = soc_dir / "soc.v"
    pcie_v.write_text("module pcie_top; endmodule\n", encoding="utf-8")
    soc_v.write_text("module soc_top; endmodule\n", encoding="utf-8")
    pcie_f.write_text(f"{pcie_v}\n", encoding="utf-8")
    soc_f.write_text(f"{soc_v}\n", encoding="utf-8")

    fl = parse_filelist(str(pcie_f))
    fl2 = parse_filelist(str(soc_f))
    via = {
        str(pcie_v.resolve()): str(pcie_f.resolve()),
        str(soc_v.resolve()): str(soc_f.resolve()),
    }

    parse, ignore = partition_sources(
        [str(pcie_v.resolve()), str(soc_v.resolve())],
        [],
        filelist_patterns=["pcie_block.f"],
        file_via_filelist=via,
    )
    assert str(pcie_v.resolve()) in ignore
    assert str(soc_v.resolve()) in parse
    assert fl.source_files and fl2.source_files


def test_ignore_filelist_skips_scan_and_stubs_reference(tmp_path: Path):
    pcie_f = tmp_path / "pcie_block.f"
    soc_f = tmp_path / "soc.f"
    pcie_dir = tmp_path / "pcie_rtl"
    soc_dir = tmp_path / "soc_rtl"
    pcie_dir.mkdir()
    soc_dir.mkdir()
    (pcie_dir / "pcie.v").write_text("module pcie_top; endmodule\n", encoding="utf-8")
    (soc_dir / "soc.v").write_text(
        "module soc_top; pcie_top u_p (); endmodule\n",
        encoding="utf-8",
    )
    pcie_f.write_text(f"{pcie_dir / 'pcie.v'}\n", encoding="utf-8")
    soc_f.write_text(f"{soc_dir / 'soc.v'}\n", encoding="utf-8")
    top_f = tmp_path / "top.f"
    top_f.write_text(f"-f {pcie_f.name}\n-f {soc_f.name}\n", encoding="utf-8")

    fl = parse_filelist(str(top_f))
    index = build_design_index(
        fl,
        ignore_paths=[],
        ignore_path_files=[],
        ignore_modules=[],
        ignore_filelists=["pcie_block.f"],
        jobs=1,
    )
    assert index.get_module("soc_top") is not None
    assert index.get_module("soc_top").stop_reason == ""
    assert index.get_module("pcie_top").stop_reason == "ignorePath"

    _, rows = elaborate(index, "soc_top")
    assert any(r.full_path == "soc_top.u_p" and r.stop_reason == "ignorePath" for r in rows)