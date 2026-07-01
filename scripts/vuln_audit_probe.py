#!/usr/bin/env python3
"""Ad-hoc connectivity vulnerability probe (not part of regression suite)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from hierwalk.connect.session import check_connectivity
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex


@dataclass
class Probe:
    case_id: str
    category: str
    severity: str  # FP / FN / LIMIT / OK
    ep_a: str
    ep_b: str
    expect: bool
    rtl: str
    top: str = "probe_top"
    defines: dict | None = None
    ff_barrier: bool = False
    strict_generate: bool = False
    note: str = ""


def _run(p: Probe) -> tuple[bool, bool, str]:
    with tempfile.TemporaryDirectory(prefix="hierwalk_probe_") as tmp:
        path = Path(tmp) / "probe.v"
        path.write_text(p.rtl, encoding="utf-8")
        index = DesignIndex.build({str(path): p.rtl})
        _root, rows = elaborate(index, p.top)
        r = check_connectivity(
            p.ep_a,
            p.ep_b,
            rows=rows,
            index=index,
            top=p.top,
            defines=p.defines or {},
            strict_generate=p.strict_generate,
            ff_barrier=p.ff_barrier,
        )
        ok = r.connected == p.expect
        return r.connected, ok, r.note or ""


def _mod(name: str, body: str) -> str:
    return f"module {name} (input src, output dst);\n{body}\nendmodule\n"


def build_probes() -> List[Probe]:
    probes: List[Probe] = []

    def add(
        case_id: str,
        category: str,
        severity: str,
        body: str,
        expect: bool,
        *,
        note: str = "",
        ff_barrier: bool = False,
        defines: dict | None = None,
        top_body: str = "",
    ):
        rtl = _mod("probe_top", body)
        if top_body:
            rtl = top_body
        probes.append(
            Probe(
                case_id=case_id,
                category=category,
                severity=severity,
                ep_a="probe_top.src",
                ep_b="probe_top.dst",
                expect=expect,
                rtl=rtl,
                ff_barrier=ff_barrier,
                defines=defines,
                note=note,
            )
        )

    # --- J series: branch / generate / FF ---
    add(
        "J9",
        "branch_over_approx",
        "FP",
        """logic q;
           always_ff @(posedge clk) if (opaque) q <= src;
           assign dst = q;""",
        expect=False,
        ff_barrier=False,
        note="FF if without else — then arm may leak",
    )
    add(
        "J20",
        "unresolved_gen",
        "FP",
        """wire [3:0] c;
           assign c[0] = src;
           generate for (gi=1; gi<4; gi=gi+1) assign c[gi]=c[gi-1]; endgenerate
           assign dst = c[3];""",
        expect=True,
        note="missing genvar keyword — wrong unroll",
    )
    add(
        "J35",
        "preprocess",
        "FP",
        """wire link;
           `ifdef GHOST assign link=src; `else assign link=1'b0; `endif
           assign dst = link;""",
        expect=False,
        defines={},
        note="single-line ifdef keeps both branches",
    )
    add(
        "J35b",
        "preprocess",
        "OK",
        """wire link;
           `ifdef GHOST
             assign link=src;
           `else
             assign link=1'b0;
           `endif
           assign dst = link;""",
        expect=False,
        defines={},
        note="multi-line ifdef — else only",
    )
    add(
        "J25",
        "body_missing",
        "OK",
        """wire mid;
           bb u (.p(src), .q(mid));
           assign dst = mid;""",
        expect=True,
        top_body="""
        module bb(input p, output q); endmodule
        module probe_top(input src, output dst);
          wire mid;
          bb u (.p(src), .q(mid));
          assign dst = mid;
        endmodule
        """,
        note="blackbox port passthrough — intentional structural link (same as B4)",
    )

    # --- K series: param / index ---
    add(
        "K13",
        "signal_granularity",
        "FN",
        """parameter P=0;
           assign dst = src[P];""",
        expect=True,
        note="scalar src vs src[0] not aligned",
    )

    # --- L series: concat / select ---
    add(
        "L2",
        "signal_granularity",
        "FP",
        "assign dst = {src,src}[2];",
        expect=False,
        note="OOB concat select falls back to roots",
    )
    add(
        "L2b",
        "signal_granularity",
        "OK",
        "assign dst = {src,1'b0}[1];",
        expect=False,
        note="valid high-bit select — no src",
    )

    # --- M series: algebraic / builtins ---
    add("M1", "cross_scope_expr", "FP", "assign dst = src + 1'b0;", expect=False)
    add("M2", "cross_scope_expr", "FP", "assign dst = src - src;", expect=False)
    add(
        "M4",
        "cross_scope_expr",
        "OK",
        "assign dst = |src;",
        expect=True,
        note="reduction OR — structural use of src",
    )
    add(
        "M7",
        "signal_granularity",
        "OK",
        "assign dst = {2{src}}[1];",
        expect=True,
        note="valid replicate index — bit 1 is src",
    )
    add("M9", "cross_scope_expr", "FP", "assign dst = $bits(src);", expect=False)

    # --- Additional probes ---
    add(
        "N1",
        "branch_over_approx",
        "LIMIT",
        """logic link;
           always_comb case (sel) 2'b00: link=src; default: link=1'b0; endcase
           assign dst = link;""",
        expect=False,
        note="unresolved comb case — union?",
    )
    add(
        "N2",
        "structural_timing",
        "OK",
        """logic q;
           always_ff @(posedge clk) case(sel) 2'b00: q<=src; default: q<=1'b0; endcase
           assign dst = q;""",
        expect=False,
        ff_barrier=True,
        note="FF unresolved case unions arms",
    )
    add(
        "N3",
        "excluded_syntax",
        "OK",
        """wire a,b;
           assign a=src;
           bufif1(a,b,1'b1);
           assign dst=b;""",
        expect=False,
        note="primitive unsupported",
    )
    add(
        "N4",
        "cross_scope_expr",
        "FP",
        "assign dst = src * 1'b1;",
        expect=False,
        note="multiply identity",
    )
    add(
        "N5",
        "cross_scope_expr",
        "FP",
        "assign dst = ~src ^ ~src;",
        expect=False,
        note="double negation self-cancel variant",
    )
    add(
        "N6",
        "signal_granularity",
        "FP",
        "assign dst = src[99];",
        expect=False,
        note="OOB bit select",
    )
    add(
        "N7",
        "preprocess",
        "FP",
        """wire link;
           `define TIE assign link=src;
           `ifdef MISSING `TIE `endif
           assign link=1'b0;
           assign dst=link;""",
        expect=False,
        defines={},
        note="macro in inactive ifdef branch",
    )
    add(
        "N8",
        "branch_over_approx",
        "FP",
        """logic link;
           always_ff @(posedge clk) if(opaque) q<=src;
           logic q;
           assign dst=q;""",
        expect=False,
        ff_barrier=False,
        note="FF if-no-else variant",
    )
    add(
        "N9",
        "unresolved_gen",
        "LIMIT",
        """wire [3:0] c;
           assign c[0]=src;
           generate for(genvar gi=0; gi<4; gi++) if(gi>0) assign c[gi]=c[gi-1]; endgenerate
           assign dst=c[3];""",
        expect=True,
        note="nested if in for — should chain",
    )
    add(
        "N10",
        "signal_granularity",
        "FN",
        """assign dst = src[0];""",
        expect=True,
        note="bit0 of scalar — baseline",
    )

    return probes


def main() -> None:
    probes = build_probes()
    print(f"{'ID':<6} {'cat':<22} {'sev':<6} {'exp':<5} {'got':<5} {'ok':<4} note")
    print("-" * 90)
    bugs: List[Probe] = []
    confirmed: List[Probe] = []
    for p in probes:
        got, ok, note = _run(p)
        mark = "OK" if ok else "BUG"
        if not ok and p.severity in ("FP", "FN"):
            bugs.append(p)
        elif ok and p.severity in ("FP", "FN"):
            confirmed.append(p)
        print(
            f"{p.case_id:<6} {p.category:<22} {p.severity:<6} "
            f"{str(p.expect):<5} {str(got):<5} {mark:<4} {p.note or note}"
        )
    print()
    print(f"Total probes: {len(probes)}")
    print(f"Confirmed vulnerabilities (severity FP/FN, result mismatch): {len(bugs)}")
    for b in bugs:
        kind = "FALSE POSITIVE" if b.severity == "FP" else "FALSE NEGATIVE"
        print(f"  [{b.case_id}] {kind}: {b.note}")
    print(f"Fixed since audit label (labeled FP/FN but now OK): {len(confirmed)}")
    for c in confirmed:
        print(f"  [{c.case_id}] was {c.severity}, now passes")


if __name__ == "__main__":
    main()