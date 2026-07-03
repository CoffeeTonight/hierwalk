#!/usr/bin/env python3
"""Regression sweep for confident scoped resolve (nested FL / stub / ifndef)."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import PathWalkState, run_path_walk_connect

PARENT_RTL = {
    "top.a": "top_a.v",
    "SOC_TOP.u_blk": "blk_real.v",
}


@dataclass(frozen=True)
class Case:
    name: str
    builder: Callable[[Path], tuple[Path, str, Optional[str]]]
    expect_path: str
    expect_parent_path: Optional[str] = None
    expect_parent_rtl: Optional[str] = None
    expect_child_rtl: Optional[str] = None
    no_recovery: bool = False


def _run_case(case: Case) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fl_path, target, top = case.builder(tmp)
        top_name = top or "top"
        fl = parse_filelist(str(fl_path), index_cwd=str(tmp))
        req = ConnectivityRequest(
            checks=(ConnectivityCheck(target, target, check_id="1"),),
            top=top_name,
        )
        if case.no_recovery:

            def _no_recovery(self, spec_targets=None, **kwargs):
                return 0, 0, []

            PathWalkState.run_recovery_pass = _no_recovery  # type: ignore[method-assign]

        batch, _index, state = run_path_walk_connect(
            req,
            fl,
            top=top_name,
            no_cache=True,
            connect_phase="text",
        )
        row = state.rows_by_path.get(case.expect_path)
        if row is None:
            return False, f"missing row {case.expect_path!r}: {batch.results[0].errors}"
        if not batch.results[0].connected:
            return False, f"not connected: {batch.results[0].errors}"
        if state.mod_db.defer_count() != 0:
            return False, f"defer_count={state.mod_db.defer_count()}"
        if case.expect_parent_path:
            parent = state.rows_by_path.get(case.expect_parent_path)
            if parent is None:
                return False, f"missing parent path {case.expect_parent_path!r}"
            want_rtl = case.expect_parent_rtl or PARENT_RTL.get(case.expect_parent_path, "")
            if want_rtl and not str(parent.file).endswith(want_rtl):
                return False, f"parent rtl {parent.file!r} != *{want_rtl}"
        if case.expect_child_rtl and not str(row.file).endswith(case.expect_child_rtl):
            return False, f"child rtl {row.file!r} != *{case.expect_child_rtl}"
        return True, f"module={row.module} rtl={Path(row.file).name}"


def _stub_parent_nested(tmp: Path) -> tuple[Path, str, Optional[str]]:
    (tmp / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp / "top_a.v").write_text(
        "module A;\n`ifndef NO_B\n B b ();\n`endif\nendmodule\n",
        encoding="utf-8",
    )
    (tmp / "b_stub.v").write_text("module A; endmodule\nmodule B; endmodule\n", encoding="utf-8")
    lists = tmp / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(str((tmp / "b_stub.v").resolve()) + "\n")
    (lists / "parent.f").write_text(f"-f {(lists / 'child.f').resolve()}\n")
    root = tmp / "root.f"
    root.write_text(
        "\n".join(
            [
                str((tmp / "top.v").resolve()),
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp / "top_a.v").resolve()),
            ]
        )
        + "\n",
    )
    return root, "top.a.b", "top"


def _deep_4level_fl(tmp: Path) -> tuple[Path, str, Optional[str]]:
    (tmp / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp / "top_a.v").write_text("module A; B b (); endmodule\n", encoding="utf-8")
    (tmp / "b_stub.v").write_text("module B; endmodule\n", encoding="utf-8")
    lists = tmp / "lists"
    lists.mkdir()
    (lists / "grandchild.f").write_text(str((tmp / "b_stub.v").resolve()) + "\n")
    (lists / "child.f").write_text(f"-f {(lists / 'grandchild.f').resolve()}\n")
    (lists / "parent.f").write_text(f"-f {(lists / 'child.f').resolve()}\n")
    root = tmp / "root.f"
    root.write_text(
        "\n".join(
            [
                str((tmp / "top.v").resolve()),
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp / "top_a.v").resolve()),
            ]
        )
        + "\n",
    )
    return root, "top.a.b", "top"


def _nested_ifndef_port_map(tmp: Path) -> tuple[Path, str, Optional[str]]:
    (tmp / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp / "top_a.v").write_text(
        "module A;\n`ifndef NO_B\n B b (\n`ifndef PORT_X\n .a(w),\n`endif\n );\n`endif\nendmodule\n",
        encoding="utf-8",
    )
    (tmp / "b_stub.v").write_text("module B(input a); endmodule\n", encoding="utf-8")
    lists = tmp / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(str((tmp / "b_stub.v").resolve()) + "\n")
    (lists / "parent.f").write_text(f"-f {(lists / 'child.f').resolve()}\n")
    root = tmp / "root.f"
    root.write_text(
        "\n".join(
            [
                str((tmp / "top.v").resolve()),
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp / "top_a.v").resolve()),
            ]
        )
        + "\n",
    )
    return root, "top.a.b", "top"


def _colist_on_parent_fl(tmp: Path) -> tuple[Path, str, Optional[str]]:
    (tmp / "top_a.v").write_text(
        "module A; B b (); endmodule\nmodule top; A a (); endmodule\n",
        encoding="utf-8",
    )
    (tmp / "b_stub.v").write_text("module B; endmodule\n", encoding="utf-8")
    lists = tmp / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(str((tmp / "b_stub.v").resolve()) + "\n")
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp / "top_a.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
    )
    root = tmp / "root.f"
    root.write_text(f"-f {(lists / 'parent.f').resolve()}\n")
    return root, "top.a.b", "top"


def _soc_stub_chain(tmp: Path) -> tuple[Path, str, Optional[str]]:
    (tmp / "top.v").write_text("module SOC_TOP; BLK u_blk (); endmodule\n", encoding="utf-8")
    (tmp / "blk_stub.v").write_text("module BLK; endmodule\n", encoding="utf-8")
    (tmp / "blk_real.v").write_text("module BLK; CORE u_core (); endmodule\n", encoding="utf-8")
    (tmp / "core.v").write_text("module CORE; LEAF u_leaf (); endmodule\n", encoding="utf-8")
    (tmp / "leaf.v").write_text("module LEAF; endmodule\n", encoding="utf-8")
    lists = tmp / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(str((tmp / "blk_stub.v").resolve()) + "\n")
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp / "top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
    )
    root = tmp / "root.f"
    root.write_text(
        "\n".join(
            [
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp / "blk_real.v").resolve()),
                str((tmp / "core.v").resolve()),
                str((tmp / "leaf.v").resolve()),
            ]
        )
        + "\n",
    )
    return root, "SOC_TOP.u_blk.u_core.u_leaf", "SOC_TOP"


CASES: List[Case] = [
    Case(
        "stub-parent-nested-no-recovery",
        _stub_parent_nested,
        "top.a.b",
        expect_parent_path="top.a",
        expect_child_rtl="b_stub.v",
        no_recovery=True,
    ),
    Case(
        "deep-4level-fl-no-recovery",
        _deep_4level_fl,
        "top.a.b",
        expect_parent_path="top.a",
        expect_child_rtl="b_stub.v",
        no_recovery=True,
    ),
    Case(
        "colist-on-parent-fl",
        _colist_on_parent_fl,
        "top.a.b",
        expect_parent_path="top.a",
        expect_child_rtl="b_stub.v",
        no_recovery=True,
    ),
    Case(
        "soc-stub-chain-no-recovery",
        _soc_stub_chain,
        "SOC_TOP.u_blk.u_core.u_leaf",
        expect_parent_path="SOC_TOP.u_blk",
        expect_child_rtl="leaf.v",
        no_recovery=True,
    ),
    Case(
        "nested-ifndef-port-map",
        _nested_ifndef_port_map,
        "top.a.b",
        expect_parent_path="top.a",
        expect_child_rtl="b_stub.v",
        no_recovery=True,
    ),
]


def main() -> int:
    failed = 0
    for case in CASES:
        ok, detail = _run_case(case)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case.name}: {detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())