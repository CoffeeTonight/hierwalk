"""Run JSON env → filelist $VAR expansion."""

from __future__ import annotations

from pathlib import Path

from pyhirewalk.filelist.expand import expand_filelist
from pyhirewalk.run_config import (
    expand_env_string,
    load_run_config,
    parse_env_block,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_expand_env_string_braces_and_plain() -> None:
    s = expand_env_string(
        "${PROJ}/rtl/$CHIP/filelist.f",
        {"PROJ": "/work", "CHIP": "top"},
    )
    assert s == "/work/rtl/top/filelist.f"


def test_filelist_uses_env_for_nested_paths(tmp_path: Path) -> None:
    rtl = tmp_path / "proj" / "rtl"
    src = rtl / "a.v"
    _write(src, "module a; endmodule\n")
    nested = rtl / "ip.f"
    _write(nested, "a.v\n")
    top_f = tmp_path / "top.f"
    # $PROJ used inside filelist (typical EDA pattern)
    _write(top_f, "-f $PROJ/rtl/ip.f\n+define+X=1\n")

    fl = expand_filelist(
        top_f,
        env={"PROJ": str(tmp_path / "proj")},
        index_cwd=tmp_path,
    )
    assert src.resolve() in [p.resolve() for p in fl.source_files]
    assert fl.defines.get("X") == "1"


def test_run_json_env_applied_and_filelist(tmp_path: Path) -> None:
    rtl = tmp_path / "chip" / "rtl"
    _write(rtl / "t.sv", "module t; endmodule\n")
    fl_path = rtl / "design.f"
    _write(fl_path, "$RTL_ROOT/t.sv\n")

    cfg_path = tmp_path / "run.json"
    _write(
        cfg_path,
        f"""{{
      "filelist": "$CHIP_ROOT/rtl/design.f",
      "top": "t",
      "cwd": "$CHIP_ROOT",
      "env": {{
        "CHIP_ROOT": "{tmp_path / "chip"}",
        "RTL_ROOT": "{rtl}"
      }},
      "defines": {{ "SYNTHESIS": "1" }},
      "build_db": {{ "output": "out.sqlite" }}
    }}
    """,
    )

    cfg = load_run_config(cfg_path)
    assert "CHIP_ROOT" in cfg.env
    assert cfg.filelist.resolve() == fl_path.resolve()
    assert cfg.index_cwd is not None
    assert cfg.index_cwd.resolve() == (tmp_path / "chip").resolve()

    fl = expand_filelist(
        cfg.filelist,
        env=cfg.filelist_env(),
        index_cwd=cfg.index_cwd,
    )
    assert (rtl / "t.sv").resolve() in [p.resolve() for p in fl.source_files]


def test_parse_conn_checks_and_jobs(tmp_path: Path) -> None:
    from pyhirewalk.run_config import (
        hierarchy_paths_from_config,
        load_run_config,
        parse_conn_checks,
    )

    checks = parse_conn_checks(
        {
            "checks": [
                {
                    "id": "cpu",
                    "a": ["top.u_a.x", "top.u_a.y[3:0]"],
                    "b": ["top.u_b.z"],
                }
            ]
        }
    )
    assert len(checks) == 1
    assert checks[0].id == "cpu"
    assert checks[0].a == ("top.u_a.x", "top.u_a.y[3:0]")
    assert checks[0].b == ("top.u_b.z",)
    assert checks[0].a_role == "fanout"

    fl = tmp_path / "f.f"
    fl.write_text("// empty\n", encoding="utf-8")
    cfgp = tmp_path / "run.json"
    cfgp.write_text(
        """
        {
          "filelist": "f.f",
          "top": "chip",
          "jobs": 8,
          "defines": { "NO_CPU": "1" },
          "env": { "PROJ": "/p" },
          "hier_resolve": { "paths": ["chip.extra"] },
          "run_conn_check": {
            "checks": [
              { "id": "cpu", "a": ["chip.a"], "b": ["chip.b"] },
              { "id": "noc", "a": ["chip.a"], "b": ["chip.c"] }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    cfg = load_run_config(cfgp, apply_process_env=False)
    assert cfg.top == "chip"
    assert cfg.jobs == 8
    assert cfg.defines.get("NO_CPU") == "1"
    assert len(cfg.conn_checks) == 2
    assert cfg.conn_checks[0].id == "cpu"
    assert cfg.conn_checks[0].a == ("chip.a",)
    # default: ONLY checks a∪b (not hier_resolve.paths / noise)
    flat = hierarchy_paths_from_config(cfg)
    assert flat == ["chip.a", "chip.b", "chip.c"]
    # legacy opt-in
    assert hierarchy_paths_from_config(cfg, include_resolve_paths=True) == [
        "chip.a",
        "chip.b",
        "chip.c",
        "chip.extra",
    ]

    from pyhirewalk.run_config import (
        extract_hierarchies_from_run_conn_checks,
        load_hier_resolve_inputs,
        parse_conn_checks,
    )

    # noise / blabla / nested garbage must not become hierarchies
    noisy = tmp_path / "noisy.json"
    noisy.write_text(
        r"""
        {
          "filelist": "f.f",
          "paths": ["chip.should.not"],
          "hier_resolve": { "paths": ["chip.also.not"] },
          "checks": {
            "spoof": { "a": ["chip.top_level_checks"], "b": ["chip.top_b"] }
          },
          "meaningless": {
            "foo": 1,
            "bar": { "baz": ["chip.deep.noise"] },
            "quote_trap": "\"chip.quoted.noise\""
          },
          "defines": { "NO_CPU": "1" },
          "run_conn_check": {
            "blabla": {
              "a": ["chip.from.blabla"],
              "b": ["chip.from.blabla.b"],
              "should": "not"
            },
            "description": "run_conn_check:blabla:{ fake",
            "extra_list": ["chip.extra_list"],
            "nested": {
              "checks": [
                { "id": "fake", "a": ["chip.nested.checks"], "b": ["chip.nested.b"] }
              ]
            },
            "checks": [
              {
                "id": "c1",
                "a": ["chip.a", "chip.a2"],
                "b": ["chip.b"],
                "meta": { "x": ["chip.meta.noise"] },
                "comment": "not a path"
              },
              {
                "id": "c2",
                "a": ["chip.c"],
                "b": ["chip.d", "chip.e"]
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    hpaths, hdefs, _mmap = load_hier_resolve_inputs(noisy)
    assert hpaths == ["chip.a", "chip.a2", "chip.b", "chip.c", "chip.d", "chip.e"], hpaths
    assert hdefs.get("NO_CPU") == "1"
    for bad in (
        "chip.should.not",
        "chip.also.not",
        "chip.top_level_checks",
        "chip.from.blabla",
        "chip.nested.checks",
        "chip.meta.noise",
        "chip.extra_list",
        "chip.deep.noise",
        "chip.quoted.noise",
    ):
        assert bad not in hpaths, bad
    assert not any('"' in p for p in hpaths)

    # no checks array → error (do not treat blabla as checks)
    import pytest

    bare = {"run_conn_check": {"blabla": {"a": ["x"], "b": ["y"]}}}
    try:
        extract_hierarchies_from_run_conn_checks(bare)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "checks" in str(e)

    # parse_conn_checks ignores blabla sibling
    pcs = parse_conn_checks(
        {
            "blabla": {"a": ["nope"], "b": ["nope2"]},
            "checks": [{"id": "c", "a": ["only.a"], "b": ["only.b"]}],
        }
    )
    assert len(pcs) == 1 and pcs[0].a == ("only.a",)


def test_parse_env_block_skips_null() -> None:
    assert parse_env_block({"A": "1", "B": None}) == {"A": "1"}
