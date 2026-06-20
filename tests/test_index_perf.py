"""Index/preprocess performance regressions."""

from __future__ import annotations

import time

from hierwalk.index import scan_preprocessed
from hierwalk.inst_scan import iter_module_blocks, slim_body_for_instance_scan
from hierwalk.preprocess import (
    _SOURCE_PREPROCESS_CACHE,
    apply_ifdef_filter,
    clear_include_unit_cache,
    preprocess_file_for_index,
)


def test_ifdef_filter_large_text_streaming():
    line = "`ifdef ON wire a; `else wire b; `endif\n"
    text = line * 200_000
    t0 = time.perf_counter()
    out = apply_ifdef_filter(text, {"ON": "1"})
    elapsed = time.perf_counter() - t0
    assert "wire a" in out
    assert "wire b" not in out
    assert elapsed < 3.0, f"ifdef filter took {elapsed:.1f}s"


def test_iter_module_blocks_large_file():
    body = "wire x;\n" * 100_000
    text = f"module top;\n{body} leaf u0 ();\nendmodule\n"
    t0 = time.perf_counter()
    blocks = list(iter_module_blocks(text))
    elapsed = time.perf_counter() - t0
    assert len(blocks) == 1
    assert blocks[0]["name"] == "top"
    assert elapsed < 2.0, f"module iter took {elapsed:.1f}s"


def test_slim_body_drops_macro_only_lines():
    noise = "\n".join(f"`MACRO_{i}" for i in range(50_000))
    body = noise + "\nleaf u0 ();\n"
    slim = slim_body_for_instance_scan(body)
    assert "leaf u0" in slim
    assert "`MACRO_0" not in slim


def test_source_preprocess_cache_hit(tmp_path):
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "`define F 1\nmodule top; leaf u0 (); endmodule\n",
        encoding="utf-8",
    )
    clear_include_unit_cache()
    defs: dict[str, str] = {}
    preprocess_file_for_index(rtl, [tmp_path], defs)
    assert _SOURCE_PREPROCESS_CACHE
    t0 = time.perf_counter()
    preprocess_file_for_index(rtl, [tmp_path], {})
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"cache miss path took {elapsed:.3f}s"


def test_scan_macro_heavy_body(tmp_path):
    macro_lines = "\n".join(f"`CFG_{i % 50}" for i in range(80_000))
    text = f"module top;\n{macro_lines}\nleaf_a u_a ();\nleaf_b u_b ();\nendmodule\n"
    t0 = time.perf_counter()
    mods = scan_preprocessed(text, "macro.v")
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, f"scan took {elapsed:.1f}s"
    kids = {e.child_module for e in mods["top"].instances}
    assert kids == {"leaf_a", "leaf_b"}