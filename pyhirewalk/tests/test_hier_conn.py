"""hier_conn S0–S5: resolve-only seeds, multi-check structural meet."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _fixture(tmp: Path) -> tuple[Path, Path, Path]:
    rtl = tmp / "rtl"
    _write(
        rtl / "leaf_a.sv",
        "module leaf_a (input logic i, output logic o);\n"
        "  assign o = i;\n"
        "endmodule\n",
    )
    _write(
        rtl / "leaf_b.sv",
        "module leaf_b (input logic i, output logic o);\n"
        "  assign o = i;\n"
        "endmodule\n",
    )
    _write(
        rtl / "mid.sv",
        "module mid (input logic a, output logic b, output logic c);\n"
        "  logic t;\n"
        "  leaf_a u_a (.i(a), .o(t));\n"
        "  assign b = t;\n"
        "  leaf_b u_b (.i(a), .o(c));\n"
        "endmodule\n",
    )
    _write(
        rtl / "top.sv",
        "module top (input logic din, output logic x, output logic y);\n"
        "`ifdef USE_MID\n"
        "  mid u_mid (.a(din), .b(x), .c(y));\n"
        "`endif\n"
        "endmodule\n",
    )
    map_path = tmp / "essential.modules.json"
    map_path.write_text(
        json.dumps(
            {
                "modules": {
                    "top": [str(rtl / "top.sv")],
                    "mid": [str(rtl / "mid.sv")],
                    "leaf_a": [str(rtl / "leaf_a.sv")],
                    "leaf_b": [str(rtl / "leaf_b.sv")],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    run_path = tmp / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "env": {"WORK": str(tmp)},
                "defines": {"USE_MID": "1"},
                "modules_json": str(map_path),
                "run_conn_check": {
                    "blabla": {"a": ["noise.a"], "b": ["noise.b"]},
                    "checks": [
                        {
                            "id": "leaf_loop",
                            "a": ["top.u_mid.u_a.i"],
                            "b": ["top.u_mid.u_a.o"],
                        },
                        {
                            "id": "cross_mid",
                            "a": ["top.din"],
                            "b": ["top.x"],
                        },
                        {
                            "id": "no_conn",
                            "a": ["top.u_mid.u_a.o"],
                            "b": ["top.u_mid.u_b.o"],
                        },
                        {
                            "id": "miss_seed",
                            "a": ["top.does.not.exist"],
                            "b": ["top.x"],
                        },
                    ],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return run_path, map_path, tmp


def test_hier_conn_pipeline(tmp_path: Path) -> None:
    run_path, map_path, tmp = _fixture(tmp_path)
    resolve_out = tmp / "hier_resolve.json"
    conn_out = tmp / "hier_conn.json"

    r1 = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "hier_resolve.py"),
            "--config",
            str(run_path),
            "--map",
            str(map_path),
            "-o",
            str(resolve_out),
        ],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    assert r1.returncode == 0, r1.stderr
    res_doc = json.loads(resolve_out.read_text(encoding="utf-8"))
    by_path = {r["path"]: r for r in res_doc["results"]}
    # fan fields present on ok leaves
    assert by_path["top.u_mid.u_a.i"]["leaf"]["fan"] == "fanin"
    assert by_path["top.u_mid.u_a.o"]["leaf"]["fan"] == "fanout"

    r2 = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "hier_conn.py"),
            "--config",
            str(run_path),
            "--map",
            str(map_path),
            "--resolve",
            str(resolve_out),
            "-o",
            str(conn_out),
        ],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 0, r2.stderr
    assert "TOTAL_HIER_CONN_SEC" in r2.stderr

    doc = json.loads(conn_out.read_text(encoding="utf-8"))
    checks = {c["id"]: c for c in doc["checks"]}

    # T1 leaf assign
    assert len(checks["leaf_loop"]["pairs"]) == 1
    ev0 = checks["leaf_loop"]["pairs"][0]["evidence"]
    assert ev0 and "assign o = i" in ev0[0]["snippet"]

    # T2 cross module
    assert len(checks["cross_mid"]["pairs"]) == 1
    assert checks["cross_mid"]["pairs"][0]["src"] == "top.din"
    assert checks["cross_mid"]["pairs"][0]["dst"] == "top.x"
    assert len(checks["cross_mid"]["pairs"][0]["evidence"]) >= 1

    # T3 no connection
    assert checks["no_conn"]["pairs"] == []
    assert any(u.get("reason") == "no_meet" for u in checks["no_conn"]["unconnected"])

    # T4 resolve miss
    assert any(
        u.get("reason") == "resolve_miss" for u in checks["miss_seed"]["unconnected"]
    )
    assert checks["miss_seed"]["pairs"] == []


def test_hier_conn_requires_resolve(tmp_path: Path) -> None:
    run_path, map_path, _ = _fixture(tmp_path)
    r = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "hier_conn.py"),
            "--config",
            str(run_path),
            "--map",
            str(map_path),
            "-o",
            str(tmp_path / "out.json"),
        ],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
