"""`` `ifndef `` / `` `define `` include-guard order during preprocess."""

from __future__ import annotations

from pathlib import Path

from hierwalk.index import scan_preprocessed
from hierwalk.preprocess import (
    apply_ifdef_filter,
    clear_include_unit_cache,
    preprocess_file,
    preprocess_file_for_index,
)

_GUARDED = """\
`ifndef _BLA_
`define _BLA_
module BLA(input logic a);
  wire w;
endmodule
`endif
"""


def test_ifndef_define_guard_keeps_module_without_external_define(tmp_path: Path):
    rtl = tmp_path / "bla.v"
    rtl.write_text(_GUARDED, encoding="utf-8")
    clear_include_unit_cache()

    defs: dict[str, str] = {}
    text = preprocess_file(rtl, [tmp_path], defs)
    assert "module BLA" in text
    assert "_BLA_" in defs

    tier_defs: dict[str, str] = {}
    tier_text = preprocess_file_for_index(
        rtl,
        [tmp_path],
        tier_defs,
        apply_ifdef=True,
    )
    assert "module BLA" in tier_text
    mods = scan_preprocessed(tier_text, str(rtl))
    assert "BLA" in mods


def test_ifndef_define_guard_skips_module_when_external_define_set(tmp_path: Path):
    rtl = tmp_path / "bla.v"
    rtl.write_text(_GUARDED, encoding="utf-8")
    clear_include_unit_cache()

    defs = {"_BLA_": "1"}
    text = preprocess_file(rtl, [tmp_path], defs)
    assert "module BLA" not in text

    tier_defs = {"_BLA_": "1"}
    tier_text = preprocess_file_for_index(
        rtl,
        [tmp_path],
        tier_defs,
        apply_ifdef=True,
    )
    assert "module BLA" not in tier_text


def test_ifndef_define_guard_index_keeps_module_before_ifdef_filter(tmp_path: Path):
    """Index pass keeps BLA; `` `define `` must not be hoisted ahead of `` `ifndef ``."""
    rtl = tmp_path / "bla.v"
    rtl.write_text(_GUARDED, encoding="utf-8")
    clear_include_unit_cache()

    defs: dict[str, str] = {}
    index_text = preprocess_file_for_index(rtl, [tmp_path], defs)
    assert "module BLA" in index_text
    assert "`ifndef _BLA_" in index_text
    assert "_BLA_" in defs