"""ignore-path pattern matching and fast stub scan."""

from __future__ import annotations

from pathlib import Path

from hierwalk.ignore_path import (
    normalized_ignore_path,
    partition_sources,
    source_path_matches,
)
from hierwalk.index import DesignIndex


def test_source_path_matches_folder_segment():
    path = "/proj/rtl/pcielinktop/foo.v"
    assert source_path_matches(path, ["pcielinktop"])
    assert not source_path_matches(path, ["pcieinnktop"])


def test_source_path_matches_rtl_file_glob():
    assert source_path_matches("/proj/rtl/DW_blabla.v", ["DW_*"])
    assert not source_path_matches("/proj/rtl/dw_blabla.v", ["DW_*"])


def test_ignore_path_skips_preprocess_for_vendor_tree(tmp_path):
    vendor = tmp_path / "pcielinktop"
    vendor.mkdir()
    slow = vendor / "big.v"
    slow.write_text(
        "`include \"missing.vh\"\nmodule big; child u ( ); endmodule\n",
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text(
        "module top; big u_big ( ); endmodule\n",
        encoding="utf-8",
    )

    sources = [str(top), str(slow)]
    index = DesignIndex.build_from_sources(
        sources,
        include_dirs=[],
        defines={},
        jobs=1,
        ignore_paths=["pcielinktop"],
    )
    assert index.get_module("big").stop_reason == "ignorePath"
    assert index.get_module("top") is not None
    assert index.get_module("top").stop_reason == ""


def test_ignore_path_case_insensitive_folder_segment():
    path = "/proj/rtl/PCIeLinkTop/foo.v"
    assert source_path_matches(path, ["pcielinktop"])
    assert source_path_matches(path, ["pciephytop"]) is False


def test_partition_sources_uses_resolved_absolute_path(tmp_path):
    vendor = tmp_path / "pcielinktop"
    vendor.mkdir()
    rtl = vendor / "ip.v"
    rtl.write_text("module ip; endmodule\n", encoding="utf-8")
    rel = Path("pcielinktop") / "ip.v"
    parse, ignore = partition_sources([str(tmp_path / rel)], ["pcielinktop"])
    assert ignore == [str(rtl.resolve())]
    assert not parse


def test_preprocess_skips_ignore_path_includes(tmp_path):
    vendor = tmp_path / "pcielinktop"
    vendor.mkdir()
    defs = vendor / "pcie_link_user_defs.v"
    nested = vendor / "nested.vh"
    nested.write_text("\n".join(f"`define N{i} 1" for i in range(2000)), encoding="utf-8")
    defs.write_text('`include "nested.vh"\n`define PCIE_LINK 1\n', encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text(
        '`include "pcie_link_user_defs.v"\nmodule top; endmodule\n',
        encoding="utf-8",
    )

    from hierwalk.preprocess import clear_include_unit_cache, preprocess_sources

    clear_include_unit_cache()
    out = preprocess_sources(
        [str(top)],
        [vendor],
        {},
        jobs=1,
        skip_path_patterns=["pcielinktop"],
    )
    text = out[str(top.resolve())]
    assert "ignore-path skipped" in text
    assert "PCIE_LINK" not in text
    assert normalized_ignore_path(defs).endswith("pcielinktop/pcie_link_user_defs.v")


def test_ignore_path_no_read_when_only_referenced(tmp_path):
    vendor = tmp_path / "PCIeLinkTop"
    vendor.mkdir()
    slow = vendor / "big.v"
    slow.write_text(
        "`include \"missing.vh\"\nmodule big; endmodule\n",
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text("module top; big u_big ( ); endmodule\n", encoding="utf-8")

    index = DesignIndex.build_from_sources(
        [str(top), str(slow)],
        include_dirs=[],
        defines={},
        jobs=1,
        ignore_paths=["pcielinktop"],
    )
    assert index.get_module("big").stop_reason == "ignorePath"


def test_partition_sources_splits_ignore(tmp_path):
    a = tmp_path / "pcieinnktop" / "a.v"
    b = tmp_path / "soc" / "b.v"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_text("module a; endmodule\n", encoding="utf-8")
    b.write_text("module b; endmodule\n", encoding="utf-8")
    parse, ignore = partition_sources(
        [str(a), str(b)],
        ["pcielinktop", "pciephyyop"],
    )
    assert str(b) in parse
    assert str(a) in parse  # pcieinnktop not matched by those patterns