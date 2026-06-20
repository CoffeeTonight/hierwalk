"""Include discovery / warm policy tests."""

from __future__ import annotations

from hierwalk.preprocess import (
    _collect_include_closure,
    _warm_include_cache_for_sources,
)


def test_collect_include_closure_stops_at_max_includes(tmp_path):
    incs = []
    for i in range(5):
        p = tmp_path / f"inc_{i}.vh"
        p.write_text(f"`define INC_{i}\n", encoding="utf-8")
        incs.append(p)
    src = tmp_path / "top.v"
    body = "\n".join(f'`include "{p.name}"' for p in incs)
    src.write_text(body + "\nmodule top; endmodule\n", encoding="utf-8")

    closure, _ = _collect_include_closure(
        [src],
        [tmp_path],
        max_includes=3,
    )
    assert len(closure) == 3


def test_no_include_warm_skips_discovery_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("HIERWALK_NO_INCLUDE_WARM", "1")
    src = tmp_path / "m.v"
    src.write_text('`include "missing.vh"\nmodule m; endmodule\n', encoding="utf-8")

    lines: list[str] = []

    warmed = _warm_include_cache_for_sources(
        [src],
        [tmp_path],
        {},
        on_progress=lines.append,
    )
    assert warmed == 0
    assert any("skip include warm" in line for line in lines)
    assert any("HIERWALK_NO_INCLUDE_WARM" in line for line in lines)
    assert not any("include discovery" in line for line in lines)