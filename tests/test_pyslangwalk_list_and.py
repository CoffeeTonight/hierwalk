"""pyslangwalk hierarchy uses AND list semantics (like hgrep)."""

from __future__ import annotations

from pathlib import Path

import pytest

pyslang = pytest.importorskip("pyslang")

from hierwalk.connect.pyslang_walk_gate import run_pyslangwalk_connect_batch
from hierwalk.connect.shared.request import parse_connect_request_json


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def _rtl(tmp: Path) -> str:
    return _write(
        tmp,
        "xa.v",
        """
        module leaf_c (output logic o, input logic c);
          assign o = c;
        endmodule
        module leaf_r (output logic o, input logic r);
          assign o = r;
        endmodule
        module leaf_f (output logic o, input logic f);
          assign o = f;
        endmodule
        module mid_d (output logic r, input logic e);
          leaf_r e (.r(e), .o(r));
        endmodule
        module mid_h (output logic o, input logic g);
          leaf_f g (.f(g), .o(o));
        endmodule
        module xa;
          logic c, o, e, r, g, f;
          leaf_c b (.c(c), .o(o));
          mid_d d (.e(c), .r(r));
          mid_h h (.g(c), .o(f));
        endmodule
        """,
    )


def test_list_and_hierarchy_all_must_pass(tmp_path: Path):
    rtl = _rtl(tmp_path)
    req = parse_connect_request_json(
        {
            "top": "xa",
            "checks": [
                {
                    "id": "ok",
                    "a": ["xa.b.c", "xa.d.e.r"],
                    "b": ["xa.h.g.f"],
                },
                {
                    "id": "a_miss",
                    "a": ["xa.b.c", "xa.NOPE"],
                    "b": ["xa.h.g.f"],
                },
            ],
        }
    )
    batch, _, _ = run_pyslangwalk_connect_batch(
        req,
        [rtl],
        top="xa",
        connect_output_dir=tmp_path / "db",
        text_coi=False,
    )
    by = {r.check_id: r for r in batch.results}
    assert by["ok"].connected
    assert by["ok"].mode == "pyslangwalk"
    notes = "\n".join(by["ok"].walk_notes or [])
    assert "pyslang-ep a[0] PASS" in notes
    assert "pyslang-ep a[1] PASS" in notes
    # List-form b (even single element) uses b[0] so notes match [path] display.
    assert "pyslang-ep b[0] PASS" in notes

    assert not by["a_miss"].connected
    notes_m = "\n".join(by["a_miss"].walk_notes or [])
    assert "pyslang-ep a[0] PASS" in notes_m
    assert "pyslang-ep a[1] FAIL" in notes_m and "NOPE" in notes_m
    assert "pyslang-ep b[0] PASS" in notes_m
    assert any("NOPE" in e for e in (by["a_miss"].errors or []))
    # Parent is hierarchy fail — not a partial expand pass
    assert by["a_miss"].mode == "pyslangwalk"
    assert not by["a_miss"].sub_results


def test_parse_list_display_strips_python_str_list_quotes():
    from hierwalk.connect.shared.expand import parse_list_display_spec

    assert parse_list_display_spec("[xa.b.c, xa.d.e.r]") == ("xa.b.c", "xa.d.e.r")
    # Accidental str(list) from older parsers
    assert parse_list_display_spec("['xa.b.c', 'xa.d.e.r']") == (
        "xa.b.c",
        "xa.d.e.r",
    )
    assert parse_list_display_spec('["xa.b.c", "xa.NOPE"]') == (
        "xa.b.c",
        "xa.NOPE",
    )


def test_path_walk_pyslangwalk_keeps_notes_and_and_fail(tmp_path: Path):
    """Two-stage path_walk: survivors keep pyslang-ep notes; AND fail skips text."""
    from hierwalk.filelist import parse_filelist
    from hierwalk.path_walk import run_path_walk_connect

    rtl = _rtl(tmp_path)
    (tmp_path / "filelist.f").write_text(Path(rtl).name + "\n", encoding="utf-8")
    fl = parse_filelist(str(tmp_path / "filelist.f"), index_cwd=str(tmp_path))
    req = parse_connect_request_json(
        {
            "top": "xa",
            "include_ff": True,
            "checks": [
                {"id": "ok", "a": "xa.b.c", "b": "xa.h.g.f"},
                {
                    "id": "and_fail",
                    "a": ["xa.b.c", "xa.NOPE"],
                    "b": ["xa.h.g.f"],
                },
            ],
        }
    )
    progress: list[str] = []
    batch, _index, _state = run_path_walk_connect(
        req,
        fl,
        top="xa",
        connect_phase="pyslangwalk",
        connect_output_dir=tmp_path / "db",
        connect_output_name="conn.tsv",
        no_cache=True,
        on_progress=progress.append,
    )
    by = {r.check_id: r for r in batch.results}
    assert by["ok"].connected
    assert by["ok"].mode == "pyslangwalk+text"
    assert any(str(n).startswith("pyslang-ep") for n in (by["ok"].walk_notes or []))
    assert not by["and_fail"].connected
    assert by["and_fail"].mode == "pyslangwalk"
    assert not by["and_fail"].sub_results
    assert any("a[1] FAIL" in n for n in (by["and_fail"].walk_notes or []))
    assert any("connect-pyslangwalk begin" in m for m in progress)
    assert any("hierarchy-gate pass=" in m for m in progress)


def test_flat_suite_connect_phase_pyslangwalk_schedules(tmp_path: Path):
    from hierwalk.run_tests import parse_flat_run_suite, expand_suite_verification_plan
    from hierwalk.run_tests import build_test_run_configs

    rtl = _rtl(tmp_path)
    (tmp_path / "filelist.f").write_text(Path(rtl).name + "\n", encoding="utf-8")
    doc = {
        "filelist": "filelist.f",
        "top": "xa",
        "index-cwd": str(tmp_path),
        "run_conn_check": {
            "enable": 1,
            "mode": "path-walk",
            "connect_phase": "pyslangwalk",
            "checks": [{"id": "c", "a": "xa.b.c", "b": "xa.h.g.f"}],
        },
    }
    suite = parse_flat_run_suite(doc, raw_text=None, base_dir=tmp_path)
    assert suite.tests and suite.tests[0].enabled
    plan = build_test_run_configs(suite, doc, base_dir=tmp_path)
    expanded = expand_suite_verification_plan(plan)
    assert len(expanded) == 1
    _entry, cfg = expanded[0]
    assert cfg.verification_phase == "pyslangwalk"
    assert cfg.mode == "check-pyslangwalk"
