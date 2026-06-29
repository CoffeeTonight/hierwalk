"""Suite verifier: logical-phase positive conn defaults."""

from __future__ import annotations

from hierwalk.suite_conn_policy import CONN_VERDICT_SKIP_IDS
from hierwalk.suite_report_verify import (
    _expected_conn_outcomes,
    _hierarchy_covers_path,
    _summarize_conn_outcomes,
)


def test_logical_phase_defaults_positive_when_unspecified():
    spec = {
        "checks": [
            {"id": "ok_check", "a": "top.a", "b": "top.b"},
            {"id": "bad_check", "a": "top.x", "b": "top.y", "expect_connected": False},
            {"id": "zz_list_display", "a": "[top.u0]", "b": "top.z"},
        ]
    }
    logical = _expected_conn_outcomes(spec, phase="logical")
    assert logical["ok_check"] is True
    assert logical["bad_check"] is False
    assert "zz_list_display" not in logical

    text = _expected_conn_outcomes(spec, phase="text")
    assert text == {"bad_check": False}
    assert "ok_check" not in text


def test_conn_verdict_skip_ids_shared_with_zigzag():
    from hierwalk.zigzag_torture_gen import SUITE_CONN_VERDICT_SKIP_IDS

    assert SUITE_CONN_VERDICT_SKIP_IDS is CONN_VERDICT_SKIP_IDS


def test_hierarchy_covers_path_requires_check_id_match():
    rows = [
        {
            "check_id": "other_check",
            "side": "a",
            "path": "top.u_foo",
            "kind": "inst",
            "status": "hit",
        },
        {
            "check_id": "want_check",
            "side": "a",
            "path": "top.u_bar",
            "kind": "inst",
            "status": "hit",
        },
    ]
    assert not _hierarchy_covers_path(
        rows, path="top.u_foo", side="a", check_id="want_check"
    )
    assert _hierarchy_covers_path(
        rows, path="top.u_bar", side="a", check_id="want_check"
    )


def test_text_conn_summary_skips_positive_disconnect_noise(tmp_path):
    tsv = tmp_path / "conn.text.tsv"
    tsv.write_text(
        "check_id\tendpoint_a\tendpoint_b\tconnected_text\n"
        "ok_check\ta\tb\tfalse\n"
        "bad_check\tx\ty\ttrue\n",
        encoding="utf-8",
    )
    spec = {
        "checks": [
            {"id": "ok_check", "expect_connected": True},
            {"id": "bad_check", "expect_connected": False},
        ]
    }
    stats, errors = _summarize_conn_outcomes(tsv, phase="text", spec=spec)
    assert stats.issues == 1
    assert len(errors) == 1
    assert errors[0].subject == "bad_check"
    assert errors[0].tag == "text bloom pass — logical should fail"


def test_text_conn_summary_skips_expected_negative_disconnect(tmp_path):
    tsv = tmp_path / "conn.text.tsv"
    tsv.write_text(
        "check_id\tendpoint_a\tendpoint_b\tconnected_text\n"
        "bad_check\tx\ty\tfalse\n",
        encoding="utf-8",
    )
    spec = {"checks": [{"id": "bad_check", "expect_connected": False}]}
    stats, errors = _summarize_conn_outcomes(tsv, phase="text", spec=spec)
    assert stats.total == 1
    assert stats.issues == 0
    assert not errors