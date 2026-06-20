"""Nested ``#(.b(2))`` instance forms — scan + generate-fold prefix."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from hierwalk.generate_fold import _prefix_instance_names
from hierwalk.inst_scan import scan_hierarchy_instances


def test_scan_hierarchy_instances_nested_named_param():
    body = "A #(.b(2)) uA (.clk(clk));"
    edges = scan_hierarchy_instances(body)
    assert len(edges) == 1
    assert edges[0].child_module == "A"
    assert edges[0].inst_name == "uA"
    assert edges[0].param_overrides == {"b": "2"}


def test_prefix_instance_names_nested_named_param():
    body = "A #(.b(2)) uA (.clk(clk));"
    out = _prefix_instance_names(body, "genblk.")
    assert out == "A #(.b(2)) genblk.uA(.clk(clk));"


def test_scan_and_prefix_multiple_named_params():
    body = "A #(.b(2),.c(2-1)) uA (.clk(clk));"
    edges = scan_hierarchy_instances(body)
    assert len(edges) == 1
    assert edges[0].param_overrides == {"b": "2", "c": "2-1"}
    out = _prefix_instance_names(body, "genblk.")
    assert out == "A #(.b(2),.c(2-1)) genblk.uA(.clk(clk));"


def test_rg_regex_vs_fixed_string_for_nested_hash_param():
    sample = "module top; A #(.b(2)) uA (.clk(clk)); endmodule\n"
    with tempfile.NamedTemporaryFile("w", suffix=".v", delete=False) as fh:
        fh.write(sample)
        path = Path(fh.name)
    try:
        broken = subprocess.run(
            ["rg", r"#(.b(2))", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        fixed = subprocess.run(
            ["rg", "-F", "#(.b(2))", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        escaped = subprocess.run(
            ["rg", r"#\(\.b\(2\)\)", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert broken.returncode != 0
        assert fixed.returncode == 0
        assert "#(.b(2))" in fixed.stdout
        assert escaped.returncode == 0
        assert re.search(r"#\(\.b\(2\)\)", escaped.stdout)
    finally:
        path.unlink(missing_ok=True)