"""Rust hw-scan optional integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.path_walk_db import tier0_regex_module_names_from_path
from hierwalk.rust_scanner import hw_scan_available, scan_file_rust


@pytest.mark.skipif(not hw_scan_available(), reason="hw-scan binary not built")
def test_scan_file_rust_modules(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        """
        module child (input wire clk, output reg q);
          wire internal;
          assign q = internal;
        endmodule
        module top;
          child u ();
        endmodule
        """,
        encoding="utf-8",
    )
    scan = scan_file_rust(rtl)
    assert scan is not None
    names = [m.name for m in scan.modules]
    assert names == ["child", "top"]
    child = scan.modules[0]
    assert "clk" in child.ports
    assert "q" in child.ports
    assert "internal" in child.wires
    assert any(a.lhs == "q" for a in child.assigns)


@pytest.mark.skipif(not hw_scan_available(), reason="hw-scan binary not built")
def test_tier0_uses_rust_when_enabled(tmp_path: Path, monkeypatch):
    rtl = tmp_path / "m.v"
    rtl.write_text("module only (); endmodule\n", encoding="utf-8")
    monkeypatch.setenv("HIERWALK_RUST_SCANNER", "1")
    names = tier0_regex_module_names_from_path(rtl)
    assert names == ["only"]