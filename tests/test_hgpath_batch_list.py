"""hgpath batch must expand JSON list endpoints (not mangle [top.a, top.b])."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.request import parse_connect_request_json
from hierwalk.inst_scan import coarse_hierarchy_path
from hgpath.batch import _collect_specs, _endpoint_specs, run_batch
from hgpath.flat_db import load_or_build_flat_db
from hgpath.path_norm import normalize_spec
from hgpath.tree_db import TreeDb, resolve_tree_db_path


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_list_display_must_not_strip_top_via_coarse():
    """Document the bug: coarse_hierarchy_path on list blob drops top."""
    blob = "[xa.b.c, xa.d.e.r]"
    mangled = coarse_hierarchy_path(blob)
    assert mangled.startswith(".")  # empty first segment after '[' strip
    # Expanded paths stay intact.
    specs = _endpoint_specs(blob)
    assert specs == ("xa.b.c", "xa.d.e.r")
    for s in specs:
        n = normalize_spec(s, top="xa")
        assert n.coarse.startswith("xa.")


def test_hg_parse_checks_list_not_python_repr():
    """hgpath parse_checks must not use str(list) → \"['a', 'b']\"."""
    from hg_core.run_config import parse_checks

    chs = parse_checks(
        {
            "checks": [
                {
                    "id": "u",
                    "a": ["xa.b.c", "xa.d.e.r"],
                    "b": ["xa.h.g.f"],
                }
            ]
        }
    )
    assert chs[0].endpoint_a == "[xa.b.c, xa.d.e.r]"
    assert not chs[0].endpoint_a.startswith("['")
    assert chs[0].expand is not None
    assert chs[0].expand.elements_a == ("xa.b.c", "xa.d.e.r")
    specs = _collect_specs(chs)
    assert specs == ["xa.b.c", "xa.d.e.r", "xa.h.g.f"]


def test_collect_specs_expands_json_list_display():
    req = parse_connect_request_json(
        {
            "top": "xa",
            "checks": [
                {
                    "id": "c1",
                    "a": ["xa.b.c", "xa.d.e.r"],
                    "b": ["xa.h.g.f"],
                }
            ],
        }
    )
    specs = _collect_specs(req.checks)
    assert "[xa" not in "".join(specs)
    assert set(specs) == {"xa.b.c", "xa.d.e.r", "xa.h.g.f"}
    for s in specs:
        assert normalize_spec(s, top="xa").coarse.startswith("xa")


def test_run_batch_list_endpoints_lpm_log_keeps_top(tmp_path: Path):
    rtl = _write(
        tmp_path,
        "xa.v",
        """
        module leaf (output logic o, input logic c, input logic d);
          assign o = c | d;
        endmodule
        module xa;
          logic c, d, o;
          leaf b (.c(c), .d(d), .o(o));
        endmodule
        """,
    )
    logs: list[str] = []
    _db, session = load_or_build_flat_db(
        [rtl], top="xa", work_dir=tmp_path / "w", refresh=True
    )
    tree = TreeDb(work_dir=tmp_path / "w", path=resolve_tree_db_path(tmp_path / "w"))
    req = parse_connect_request_json(
        {
            "top": "xa",
            "checks": [
                {
                    "id": "user",
                    # Multi-element list (display form [xa.b.c, xa.b.d]) must
                    # expand before LPM log / resolve — not strip top to .b.c…
                    "a": ["xa.b.c", "xa.b.d"],
                    "b": ["xa.b.o"],
                }
            ],
        }
    )
    # Prove display blob would be mangled if passed through coarse directly.
    display_a = str(req.checks[0].endpoint_a)
    assert display_a.startswith("[")
    assert coarse_hierarchy_path(display_a).startswith(".")

    batch = run_batch(
        req.checks,
        top="xa",
        session=session,
        tree=tree,
        on_log=logs.append,
    )
    lpm_or_resolve = [
        ln for ln in logs if "lpm spec=" in ln or "resolve spec=" in ln
    ]
    joined = "\n".join(lpm_or_resolve)
    assert "lpm spec='.b" not in joined
    assert "resolve spec='.b" not in joined
    assert "spec='[" not in joined
    assert any("xa.b.c" in ln for ln in lpm_or_resolve)
    assert any("xa.b.d" in ln for ln in lpm_or_resolve)
    assert any("xa.b.o" in ln for ln in lpm_or_resolve)
    assert batch.check_results
    assert all(k.startswith("xa") and "[" not in k for k in batch.entries)
    # All three paths should resolve on this design.
    assert all(e.ok for e in batch.entries.values())
