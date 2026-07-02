"""Include resolution and path-walk closure digest guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hierwalk.preprocess import _resolve_include, clear_resolve_include_cache


def test_resolve_include_avoids_resolve_until_hit(tmp_path: Path):
    inc = tmp_path / "pkg.vh"
    inc.write_text("`define PKG 1\n", encoding="utf-8")
    rtl = tmp_path / "top.v"
    rtl.write_text(f'`include "{inc.name}"\n', encoding="utf-8")
    clear_resolve_include_cache()

    resolve_calls = 0
    real_resolve = Path.resolve

    def counting_resolve(self: Path, *args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve(self, *args, **kwargs)

    with patch.object(Path, "resolve", counting_resolve):
        hit = _resolve_include(inc.name, '"', rtl, [tmp_path])
        after_first = resolve_calls
        _resolve_include(inc.name, '"', rtl, [tmp_path])

    assert hit is not None
    assert hit.name == inc.name
    assert after_first <= 2
    assert resolve_calls == after_first


def test_pw_include_closure_digest_emits_start_before_scan(tmp_path: Path, monkeypatch):
    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb

    monkeypatch.setenv("HIERWALK_PW_INCLUDE_CLOSURE_MAX", "3")
    incs = []
    for i in range(5):
        p = tmp_path / f"inc_{i}.vh"
        p.write_text(f"`define INC_{i}\n", encoding="utf-8")
        incs.append(p)
    rtl = tmp_path / "top.v"
    rtl.write_text(
        "\n".join(f'`include "{p.name}"' for p in incs) + "\nmodule top(); endmodule\n",
        encoding="utf-8",
    )
    path = str(rtl.resolve())
    index = DesignIndex.build_from_sources(
        [path],
        include_dirs=[str(tmp_path)],
        defines={},
    )
    db = PathWalkModuleDb(
        [path],
        index,
        include_dirs=[str(tmp_path)],
        defines={},
        no_cache=True,
    )

    events: list[str] = []

    def fake_collect(sources, include_dirs, **kwargs):
        events.append("collect")
        assert kwargs.get("max_includes") == 3
        return [], 0

    with patch(
        "hierwalk.preprocess._collect_include_closure",
        side_effect=fake_collect,
    ):
        with patch(
            "hierwalk.preprocess_log.emit_pp_log",
            side_effect=lambda tag, p, **kw: events.append(
                f"pp:{tag}:{kw.get('detail', '')}"
            ),
        ):
            db._include_closure_digest(path)

    assert events[0].startswith("pp:pp-closure:start")
    assert events[1] == "collect"