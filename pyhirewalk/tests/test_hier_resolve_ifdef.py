"""`ifdef / `ifndef evaluation for hier_resolve instance scan."""

from __future__ import annotations

import json
from pathlib import Path

import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hier_resolve import (  # noqa: E402
    HierResolver,
    ModuleMap,
    apply_sv_ifdefs,
    normalize_defines,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_apply_ifndef_active_when_undefined() -> None:
    src = "`ifndef FOO\nKEEP\n`endif\n"
    assert "KEEP" in apply_sv_ifdefs(src, {})
    assert "KEEP" not in apply_sv_ifdefs(src, {"FOO": ""}).replace("\n", "")


def test_nested_ifdef_param_hash_pattern(tmp_path: Path) -> None:
    """User pattern: type, nested ifdef around #(), then instance."""
    aaa = _write(
        tmp_path / "AAA.sv",
        "module AAA #(parameter W=1)(input logic a, output logic b);\n"
        "  assign b = a;\nendmodule\n",
    )
    top = _write(
        tmp_path / "top.sv",
        "module top (input logic a, output logic b);\n"
        "`ifdef _AA_A\n"
        "AAA\n"
        "`ifdef _BB_B\n"
        "#(.W(1))\n"
        "`endif\n"
        "u00_AAA_x (\n"
        "  .a(a),\n"
        "  .b(b)\n"
        ");\n"
        "`endif\n"
        "endmodule\n",
    )
    mp = _write(
        tmp_path / "m.json",
        json.dumps(
            {
                "modules": {
                    "top": [str(top)],
                    "AAA": [str(aaa)],
                }
            }
        ),
    )
    mmap = ModuleMap.load(mp)

    # no define → instance absent
    r0 = HierResolver(mmap, defines={})
    assert r0.inst_map(str(top)) == {}
    assert r0.resolve_one("top.u00_AAA_x.b")["status"] == "miss"

    # _AA_A only → AAA u00 without #()
    r1 = HierResolver(mmap, defines=normalize_defines(["_AA_A"]))
    assert r1.inst_map(str(top)) == {"u00_AAA_x": "AAA"}
    assert r1.resolve_one("top.u00_AAA_x.b")["status"] == "ok"

    # both → still finds with #(.W(1))
    r2 = HierResolver(mmap, defines=normalize_defines(["_AA_A", "_BB_B"]))
    assert r2.inst_map(str(top)) == {"u00_AAA_x": "AAA"}
    assert r2.resolve_one("top.u00_AAA_x.b")["status"] == "ok"


def test_line_numbers_preserved_when_inactive() -> None:
    src = "L1\n`ifdef X\nL3\n`endif\nL5\n"
    out = apply_sv_ifdefs(src, {})  # X off
    assert out.count("\n") == src.count("\n")
    assert "L1" in out and "L5" in out
    assert "L3" not in out
