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


def test_parse_literal_select() -> None:
    """S7a: integer selects only; param/expr → approx."""
    sys.path.insert(0, str(_ROOT / "src"))
    from pyhirewalk.conn.scan import (  # noqa: WPS433
        BitSel,
        parse_literal_select,
        parse_net_ref,
        scan_module_file,
    )

    assert parse_literal_select("") == (None, False)
    assert parse_literal_select("[3]") == (BitSel(3, 3), False)
    assert parse_literal_select("[7:4]") == (BitSel(7, 4), False)
    assert parse_literal_select("[ 7 : 4 ]") == (BitSel(7, 4), False)
    sel, approx = parse_literal_select("[WIDTH-1:0]")
    assert sel is None and approx is True
    sel, approx = parse_literal_select("[1][0]")
    assert sel is None and approx is True

    base, s, ap = parse_net_ref("o[3:0]")
    assert base == "o" and s == BitSel(3, 0) and not ap


def test_scan_literal_bit_select_evidence(tmp_path: Path) -> None:
    """assign o[3:0]=i[7:4] → structural edge + sel meta on evidence."""
    sys.path.insert(0, str(_ROOT / "src"))
    from pyhirewalk.conn.scan import scan_module_file  # noqa: WPS433

    rtl = tmp_path / "slice.sv"
    rtl.write_text(
        "module slice_m (input logic [7:0] i, output logic [3:0] o);\n"
        "  assign o[3:0] = i[7:4];\n"
        "endmodule\n",
        encoding="utf-8",
    )
    g = scan_module_file(str(rtl), {})
    edges = g.forward.get("i") or []
    assert len(edges) >= 1
    e = next(x for x in edges if x.dst == "o")
    assert e.kind == "assign"
    assert e.evidence.get("dst_sel") == "[3:0]"
    assert e.evidence.get("src_sel") == "[7:4]"
    assert e.evidence.get("src_sels", {}).get("i") == "[7:4]"
    assert not e.evidence.get("select_approx")
    assert "assign o[3:0] = i[7:4]" in str(e.evidence.get("snippet"))


def test_scan_param_select_approx(tmp_path: Path) -> None:
    """Non-literal select → base connectivity + select_approx (no bit claim)."""
    sys.path.insert(0, str(_ROOT / "src"))
    from pyhirewalk.conn.scan import scan_module_file  # noqa: WPS433

    rtl = tmp_path / "param_sel.sv"
    rtl.write_text(
        "module psel (input logic [7:0] i, output logic [7:0] o);\n"
        "  assign o[WIDTH-1:0] = i[WIDTH-1:0];\n"
        "endmodule\n",
        encoding="utf-8",
    )
    g = scan_module_file(str(rtl), {})
    e = next(x for x in g.forward["i"] if x.dst == "o")
    assert e.evidence.get("select_approx") is True
    assert "dst_sel" not in e.evidence


def test_hier_conn_bit_slice_literal(tmp_path: Path) -> None:
    """T6 e2e: leaf slice assign meets with sel evidence."""
    rtl = tmp_path / "rtl"
    _write(
        rtl / "leaf_s.sv",
        "module leaf_s (input logic [7:0] i, output logic [3:0] o);\n"
        "  assign o[3:0] = i[7:4];\n"
        "endmodule\n",
    )
    _write(
        rtl / "top.sv",
        "module top (input logic [7:0] din, output logic [3:0] dout);\n"
        "  leaf_s u0 (.i(din), .o(dout));\n"
        "endmodule\n",
    )
    map_path = tmp_path / "essential.modules.json"
    map_path.write_text(
        json.dumps(
            {
                "modules": {
                    "top": [str(rtl / "top.sv")],
                    "leaf_s": [str(rtl / "leaf_s.sv")],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    run_path = tmp_path / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "modules_json": str(map_path),
                "defines": {},
                "run_conn_check": {
                    "checks": [
                        {
                            "id": "slice_leaf",
                            "a": ["top.u0.i"],
                            "b": ["top.u0.o"],
                        },
                        {
                            "id": "slice_cross",
                            "a": ["top.din"],
                            "b": ["top.dout"],
                        },
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    resolve_out = tmp_path / "hier_resolve.json"
    conn_out = tmp_path / "hier_conn.json"
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
    doc = json.loads(conn_out.read_text(encoding="utf-8"))
    checks = {c["id"]: c for c in doc["checks"]}

    assert len(checks["slice_leaf"]["pairs"]) == 1
    ev = checks["slice_leaf"]["pairs"][0]["evidence"]
    assert ev and "o[3:0]" in ev[0]["snippet"]
    assert ev[0].get("dst_sel") == "[3:0]"
    assert ev[0].get("src_sel") == "[7:4]" or ev[0].get("src_sels", {}).get(
        "i"
    ) == "[7:4]"

    assert len(checks["slice_cross"]["pairs"]) == 1
    assert checks["slice_cross"]["pairs"][0]["src"] == "top.din"
    assert checks["slice_cross"]["pairs"][0]["dst"] == "top.dout"
