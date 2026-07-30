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
    # a∪b from all checks, dedup, then optional hier_resolve.paths
    flat = hierarchy_paths_from_config(cfg)
    assert flat == ["chip.a", "chip.b", "chip.c", "chip.extra"]
    # checks only
    assert hierarchy_paths_from_config(cfg, include_resolve_paths=False) == [
        "chip.a",
        "chip.b",
        "chip.c",
    ]


def test_parse_env_block_skips_null() -> None:
    assert parse_env_block({"A": "1", "B": None}) == {"A": "1"}
