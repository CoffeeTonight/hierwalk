"""Top module inference."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.preprocess import preprocess_file
from hierwalk.top_find import find_top_modules, resolve_top_modules


def _index_from(rtl: str, path) -> DesignIndex:
    text = preprocess_file(path, [], {})
    return DesignIndex.build({str(path): text})


def test_find_top_excludes_instantiated(tmp_path):
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
    index = _index_from("", rtl)
    assert find_top_modules(index) == ["top"]


def test_find_top_excludes_ignore_path_stub(tmp_path):
    vendor = tmp_path / "vendor_ip"
    vendor.mkdir()
    (vendor / "ip.v").write_text(
        "module pcie_top; pcie_leaf u ( ); endmodule\nmodule pcie_leaf; endmodule\n",
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text("module soc_top; pcie_top u ( ); endmodule\n", encoding="utf-8")
    pre = {
        str(top): preprocess_file(top, [], {}),
        str(vendor / "ip.v"): preprocess_file(vendor / "ip.v", [], {}),
    }
    index = DesignIndex.build(pre, ignore_paths=["vendor_ip"])
    tops = find_top_modules(index)
    assert tops == ["soc_top"]
    assert "pcie_leaf" not in tops


def test_resolve_single_auto(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text("module only_top; endmodule\n", encoding="utf-8")
    index = _index_from("", rtl)
    assert resolve_top_modules(index, top=None) == ["only_top"]


def test_resolve_multiple_requires_top(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top_a; endmodule
module top_b; endmodule
""",
        encoding="utf-8",
    )
    index = _index_from("", rtl)
    try:
        resolve_top_modules(index, top=None)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "top_a" in str(exc) and "top_b" in str(exc)