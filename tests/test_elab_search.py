"""Index → elaborate → search pipeline."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.library_scan import scan_library_modules
from hierwalk.preprocess import preprocess_file
from hierwalk.search import search


def test_library_scan_registers_all_modules_in_file(tmp_path):
    lib = tmp_path / "lib.v"
    lib.write_text(
        """
module lib_a; endmodule
module lib_b; endmodule
""",
        encoding="utf-8",
    )
    stubs = scan_library_modules([lib], [])
    assert set(stubs) == {"lib_a", "lib_b"}
    assert all(rec.is_blackbox for rec in stubs.values())


def test_index_maps_file_and_module(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module parent;
  child u0 ( );
endmodule
module child;
  leaf u_l ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    assert set(index.modules) == {"parent", "child", "leaf"}
    assert index.file_modules[str(rtl)] == ["child", "leaf", "parent"]
    assert index.get_module("child") is not None


def test_elab_dict_stitch_and_search(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  mid u_mid0 ( );
  mid u_mid1 ( );
endmodule
module mid;
  leaf u_subTop_0 ( );
  leaf u_sub_1 ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    root, rows = elaborate(index, "top")
    assert root.module == "top"
    assert {c.inst_name for c in root.children} == {"u_mid0", "u_mid1"}
    mid0 = next(c for c in root.children if c.inst_name == "u_mid0")
    assert mid0.module == "mid"
    assert {c.inst_name for c in mid0.children} == {"u_subTop_0", "u_sub_1"}

    hits = search("subTop", root=root)
    assert len(hits) == 2
    assert {h.full_path for h in hits} == {
        "top.u_mid0.u_subTop_0",
        "top.u_mid1.u_subTop_0",
    }

    hits_all = search("u_sub", root=root)
    assert len(hits_all) == 4


def test_ignore_path_stops_elab(tmp_path):
    vendor = tmp_path / "vendor_ip"
    vendor.mkdir()
    ip = vendor / "pcie.v"
    ip.write_text(
        """
module pcie_top;
  pcie_leaf u_inner ( );
endmodule
module pcie_leaf; endmodule
""",
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text(
        """
module top;
  pcie_top u_pcie ( );
endmodule
""",
        encoding="utf-8",
    )
    from hierwalk.preprocess import preprocess_file

    pre = {
        str(top): preprocess_file(top, [], {}),
        str(ip): preprocess_file(ip, [], {}),
    }
    index = DesignIndex.build(pre, ignore_paths=["vendor_ip"])
    root, rows = elaborate(index, "top")
    paths = [r.full_path for r in rows]
    assert "top.u_pcie" in paths
    assert r.stop_reason == "ignorePath" if (r := next(x for x in rows if x.full_path == "top.u_pcie")) else False
    assert not any(p.startswith("top.u_pcie.u_inner") for p in paths)
    assert index.get_module("pcie_top").stop_reason == "ignorePath"


def test_ignore_path_file_and_module(tmp_path):
    ignore_file = tmp_path / "ignore.txt"
    ignore_file.write_text(
        """
# vendor RTL
vendor_ip
module:secret_mod
""",
        encoding="utf-8",
    )
    vendor = tmp_path / "vendor_ip"
    vendor.mkdir()
    (vendor / "ip.v").write_text("module secret_mod; leaf u ( ); endmodule\n", encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text("module top; secret_mod u ( ); endmodule\n", encoding="utf-8")
    from hierwalk.preprocess import preprocess_file

    pre = {
        str(top): preprocess_file(top, [], {}),
        str(vendor / "ip.v"): preprocess_file(vendor / "ip.v", [], {}),
    }
    index = DesignIndex.build(pre, ignore_path_files=[str(ignore_file)])
    _, rows = elaborate(index, "top")
    assert any(r.full_path == "top.u" and r.stop_reason == "ignorePath" for r in rows)
    assert not any(r.full_path.startswith("top.u.u") for r in rows)


def test_build_from_sources_default_is_two_pass(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  child u0 ( );
endmodule
module child;
  leaf u_l ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    sources = [str(rtl)]
    index = DesignIndex.build_from_sources(sources, include_dirs=[], defines={}, jobs=1)
    assert set(index.modules) == {"top", "child", "leaf"}
    assert index.low_memory is False
    assert not index._preprocessed_sources


def test_parallel_scan_matches_serial(tmp_path):
    lines = []
    for i in range(12):
        lines.append(f"module m{i}; m{(i + 1) % 12} u ( ); endmodule\n")
    rtl = tmp_path / "many.v"
    rtl.write_text("".join(lines), encoding="utf-8")
    sources = [str(rtl)]
    serial = DesignIndex.build_from_sources(
        sources, include_dirs=[], defines={}, jobs=1
    )
    parallel = DesignIndex.build_from_sources(
        sources, include_dirs=[], defines={}, jobs=4
    )
    assert set(serial.modules) == set(parallel.modules)


def test_build_from_sources_low_memory_matches_two_pass(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  child u0 ( );
endmodule
module child; endmodule
""",
        encoding="utf-8",
    )
    sources = [str(rtl)]
    normal = DesignIndex.build_from_sources(
        sources, include_dirs=[], defines={}, jobs=1, low_memory=False
    )
    fused = DesignIndex.build_from_sources(
        sources, include_dirs=[], defines={}, jobs=1, low_memory=True
    )
    assert set(normal.modules) == set(fused.modules)
    assert fused.low_memory is True
    for name in normal.modules:
        assert normal.modules[name].instances == fused.modules[name].instances


def test_parallel_index_matches_serial(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  child u0 ( );
endmodule
module child;
  leaf u_l ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    pre = {str(rtl): text}
    serial = DesignIndex.build(pre, jobs=1)
    parallel = DesignIndex.build(pre, jobs=4)
    assert set(serial.modules) == set(parallel.modules)
    assert serial.file_modules == parallel.file_modules


def test_unknown_module_stops_elab(tmp_path):
    top = tmp_path / "top.v"
    top.write_text(
        """
module top;
  missing_mod u_miss ( );
endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(top, [], {})
    index = DesignIndex.build({str(top): text})
    _, rows = elaborate(index, "top")
    miss = next(r for r in rows if r.full_path == "top.u_miss")
    assert miss.module == "missing_mod"
    assert miss.stop_reason == "unknown"
    assert miss.file == ""
    assert not any(r.full_path.startswith("top.u_miss.") for r in rows)