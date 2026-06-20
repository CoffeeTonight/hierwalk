"""Vulnerability taxonomy, remediation plan, and expected connectivity outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


class VulnGroup(str, Enum):
    """Remediation bucket — similar weaknesses handled together."""

    BRANCH_OVER_APPROX = "branch_over_approx"  # A1–A3, D2: strict generate / case labels
    SIGNAL_GRANULARITY = "signal_granularity"  # A4, D3: indexed COI / concat
    STRUCTURAL_TIMING = "structural_timing"  # A5: documented limitation
    EXCLUDED_SYNTAX = "excluded_syntax"  # B1–B2: bind / primitives
    CROSS_SCOPE_EXPR = "cross_scope_expr"  # B3, B7, D1: hier-ref / function
    BODY_MISSING = "body_missing"  # B4: blackbox / empty body
    UNRESOLVED_GEN = "unresolved_gen"  # B5: fold failure
    PREPROCESS = "preprocess"  # B6: macros / ifdef defines
    SCALE = "scale"  # C1–C2: perf / regression paths


class Remediation(str, Enum):
    FIXED = "fixed"
    MITIGATED = "mitigated"  # opt-in flag or partial fix
    DOCUMENTED = "documented"
    OPEN = "open"


@dataclass(frozen=True)
class VulnCaseSpec:
    """One labeled connectivity experiment."""

    case_id: str
    group: VulnGroup
    remediation: Remediation
    endpoint_a: str
    endpoint_b: str
    expected_default: bool
    expected_strict: bool
    defines: Tuple[Tuple[str, str], ...] = ()
    description: str = ""
    false_positive_risk: bool = False
    false_negative_risk: bool = False


# Static plan: all items from the audit, grouped for fix + verification.
VULN_PLAN: Tuple[VulnCaseSpec, ...] = (
    VulnCaseSpec(
        "A1",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_a1.src",
        "vuln_top.u_a1.dst",
        expected_default=False,
        expected_strict=False,
        description="unresolved generate if (MYSTERY): else branch or drop — no FP",
    ),
    VulnCaseSpec(
        "A2a",
        VulnGroup.PREPROCESS,
        Remediation.FIXED,
        "vuln_top.u_a2.src",
        "vuln_top.u_a2.dst",
        expected_default=True,
        expected_strict=True,
        defines=(("VULN_USE_PATH", "1"),),
        description="ifdef VULN_USE_PATH=1 with defines passed",
    ),
    VulnCaseSpec(
        "A2b",
        VulnGroup.PREPROCESS,
        Remediation.FIXED,
        "vuln_top.u_a2.src",
        "vuln_top.u_a2.dst",
        expected_default=False,
        expected_strict=False,
        defines=(),
        description="ifdef without define takes else branch (file-level defines merged)",
    ),
    VulnCaseSpec(
        "A3",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_a3.src",
        "vuln_top.u_a3.dst",
        expected_default=False,
        expected_strict=False,
        description="constant-folded always_ff case (sel=2'b01) skips dead 2'b00 arm",
    ),
    VulnCaseSpec(
        "A4",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.FIXED,
        "vuln_top.u_a4.src",
        "vuln_top.u_a4.dst",
        expected_default=False,
        expected_strict=False,
        description="array bus hop[0] vs hop[1] must not merge",
    ),
    VulnCaseSpec(
        "A5",
        VulnGroup.STRUCTURAL_TIMING,
        Remediation.FIXED,
        "vuln_top.u_a5.src",
        "vuln_top.u_a5.dst",
        expected_default=False,
        expected_strict=False,
        description="default comb-only COI (ff_barrier) blocks FF D->Q path",
    ),
    VulnCaseSpec(
        "B1",
        VulnGroup.EXCLUDED_SYNTAX,
        Remediation.FIXED,
        "vuln_top.src_bind",
        "vuln_top.u_b1.dst",
        expected_default=True,
        expected_strict=True,
        description="bind statement ties src_bind into u_b1.dst",
    ),
    VulnCaseSpec(
        "B2",
        VulnGroup.EXCLUDED_SYNTAX,
        Remediation.FIXED,
        "vuln_top.u_b2.src",
        "vuln_top.u_b2.dst",
        expected_default=True,
        expected_strict=True,
        description="primitive and gate chain",
    ),
    VulnCaseSpec(
        "B3",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_b3.src",
        "vuln_top.u_b3.dst",
        expected_default=True,
        expected_strict=True,
        description="child hidden net reaches parent dst via port-mapped bridge wire",
    ),
    VulnCaseSpec(
        "B4",
        VulnGroup.BODY_MISSING,
        Remediation.FIXED,
        "vuln_top.u_b4.src",
        "vuln_top.u_b4.dst",
        expected_default=True,
        expected_strict=True,
        description="empty module — scalar input/output port passthrough",
    ),
    VulnCaseSpec(
        "B5",
        VulnGroup.UNRESOLVED_GEN,
        Remediation.FIXED,
        "vuln_top.u_b5.src",
        "vuln_top.u_b5.dst",
        expected_default=False,
        expected_strict=False,
        defines=(),
        description="unresolved generate if without else — block dropped, no FP",
    ),
    VulnCaseSpec(
        "B6",
        VulnGroup.PREPROCESS,
        Remediation.FIXED,
        "vuln_top.u_b6.src",
        "vuln_top.u_b6.dst",
        expected_default=True,
        expected_strict=True,
        defines=(),
        description="simple macro expanded from in-file define",
    ),
    VulnCaseSpec(
        "B7",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_b7.src",
        "vuln_top.u_b7.dst",
        expected_default=False,
        expected_strict=False,
        description="constant tie-off (src & 1'b0) does not create structural COI",
    ),
    VulnCaseSpec(
        "C1",
        VulnGroup.SCALE,
        Remediation.FIXED,
        "vuln_top.u_c1.src",
        "vuln_top.u_c1.dst",
        expected_default=True,
        expected_strict=True,
        description="many decoy siblings — true path must survive",
    ),
    VulnCaseSpec(
        "C2",
        VulnGroup.SCALE,
        Remediation.FIXED,
        "vuln_top.u_c2.src",
        "vuln_top.u_c2.dst",
        expected_default=True,
        expected_strict=True,
        description="ping-pong zigzag mini regression",
    ),
    VulnCaseSpec(
        "D1",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_d1.src",
        "vuln_top.u_d1.dst",
        expected_default=True,
        expected_strict=True,
        description="assign dst = u_child.hidden hierarchical reference",
    ),
    VulnCaseSpec(
        "D2",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_d2.src",
        "vuln_top.u_d2.dst",
        expected_default=False,
        expected_strict=False,
        description="constant-folded comb case (sel=2'b01) must not take 2'b00 arm",
    ),
    VulnCaseSpec(
        "D3",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.FIXED,
        "vuln_top.u_d3.src",
        "vuln_top.u_d3.dst",
        expected_default=True,
        expected_strict=True,
        description="concat/part-select {src,0}[0] preserves src bit",
    ),
    VulnCaseSpec(
        "E1",
        VulnGroup.PREPROCESS,
        Remediation.FIXED,
        "vuln_top.u_e1.src",
        "vuln_top.u_e1.dst",
        expected_default=True,
        expected_strict=True,
        defines=(),
        description="in-module `define enables ifdef without external defines",
    ),
    VulnCaseSpec(
        "G1",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_g1.src",
        "vuln_top.u_g1.dst",
        expected_default=False,
        expected_strict=False,
        description="constant-folded comb casez (sel=2'b01) must not take 2'b?0 arm",
    ),
    VulnCaseSpec(
        "G2",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_g2.src",
        "vuln_top.u_g2.dst",
        expected_default=False,
        expected_strict=False,
        description="constant tie-off driver on shared net masks prior variable driver",
    ),
    VulnCaseSpec(
        "G3",
        VulnGroup.BODY_MISSING,
        Remediation.FIXED,
        "vuln_top.u_g3.a",
        "vuln_top.u_g3.y",
        expected_default=False,
        expected_strict=False,
        description="empty module with multiple inputs — no scalar passthrough FP",
    ),
    VulnCaseSpec(
        "G4",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_g4.src",
        "vuln_top.u_g4.dst",
        expected_default=False,
        expected_strict=False,
        description="interface instance hier-ref (u_if.sig) excluded from structural COI",
    ),
    VulnCaseSpec(
        "H2",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_h2.src",
        "vuln_top.u_h2.dst",
        expected_default=False,
        expected_strict=False,
        description="non-zero literal tie-off (1'b1) masks prior variable driver",
    ),
    VulnCaseSpec(
        "H3",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_h3.src",
        "vuln_top.u_h3.dst",
        expected_default=False,
        expected_strict=False,
        description="localparam constant tie-off masks prior variable driver",
    ),
    VulnCaseSpec(
        "H4",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_h4.src",
        "vuln_top.u_h4.dst",
        expected_default=False,
        expected_strict=False,
        description="algebraic self-cancel (src ^ src) does not create COI",
    ),
    VulnCaseSpec(
        "H5",
        VulnGroup.EXCLUDED_SYNTAX,
        Remediation.FIXED,
        "vuln_top.u_h5.src",
        "vuln_top.u_h5.dst",
        expected_default=False,
        expected_strict=False,
        description="supply0 net in AND masks structural COI",
    ),
    VulnCaseSpec(
        "H6",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_h6.src",
        "vuln_top.u_h6.dst",
        expected_default=False,
        expected_strict=False,
        description="always_comb blocking last-wins — final tie-off masks src",
    ),
    VulnCaseSpec(
        "H7",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_h7.src",
        "vuln_top.u_h7.dst",
        expected_default=False,
        expected_strict=False,
        description="unresolved comb casez selector does not union all arms",
    ),
    VulnCaseSpec(
        "H8",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_h8.src",
        "vuln_top.u_h8.dst",
        expected_default=False,
        expected_strict=False,
        description="unresolved ternary selects false arm only (no FP)",
    ),
    VulnCaseSpec(
        "H9",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_h9.src",
        "vuln_top.u_h9.dst",
        expected_default=False,
        expected_strict=False,
        description="unresolved always_comb if selects else arm only",
    ),
    VulnCaseSpec(
        "H10",
        VulnGroup.UNRESOLVED_GEN,
        Remediation.FIXED,
        "vuln_top.u_h10.src",
        "vuln_top.u_h10.dst",
        expected_default=True,
        expected_strict=True,
        description="generate for (gi=gi+1) unrolls chained assigns",
    ),
    VulnCaseSpec(
        "I11",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.FIXED,
        "vuln_top.u_i11.src",
        "vuln_top.u_i11.dst",
        expected_default=False,
        expected_strict=False,
        description="concat part-select {src,0}[1] must not take src element",
    ),
    VulnCaseSpec(
        "J35",
        VulnGroup.PREPROCESS,
        Remediation.FIXED,
        "vuln_top.u_j35.src",
        "vuln_top.u_j35.dst",
        expected_default=False,
        expected_strict=False,
        defines=(),
        description="single-line ifdef keeps only the active branch",
    ),
    VulnCaseSpec(
        "J9",
        VulnGroup.BRANCH_OVER_APPROX,
        Remediation.FIXED,
        "vuln_top.u_j9.src",
        "vuln_top.u_j9.dst",
        expected_default=False,
        expected_strict=False,
        description="always_ff if(opaque) without else does not take then arm",
    ),
    VulnCaseSpec(
        "J20",
        VulnGroup.UNRESOLVED_GEN,
        Remediation.FIXED,
        "vuln_top.u_j20.src",
        "vuln_top.u_j20.dst",
        expected_default=True,
        expected_strict=True,
        description="generate for without genvar keyword unrolls chained assigns",
    ),
    VulnCaseSpec(
        "J25",
        VulnGroup.BODY_MISSING,
        Remediation.DOCUMENTED,
        "vuln_top.u_j25.src",
        "vuln_top.u_j25.dst",
        expected_default=True,
        expected_strict=True,
        description="blackbox module with scalar ports — intentional passthrough COI",
    ),
    VulnCaseSpec(
        "K13",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.FIXED,
        "vuln_top.u_k13.src",
        "vuln_top.u_k13.dst",
        expected_default=True,
        expected_strict=True,
        description="parameter index src[P] with P=0 aligns to scalar src",
    ),
    VulnCaseSpec(
        "L2",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.FIXED,
        "vuln_top.u_l2.src",
        "vuln_top.u_l2.dst",
        expected_default=False,
        expected_strict=False,
        description="OOB concat part-select {src,src}[2] must not fall back to src roots",
    ),
    VulnCaseSpec(
        "M1",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_m1.src",
        "vuln_top.u_m1.dst",
        expected_default=False,
        expected_strict=False,
        description="algebraic identity src + 1'b0 does not create structural COI",
    ),
    VulnCaseSpec(
        "M2",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_m2.src",
        "vuln_top.u_m2.dst",
        expected_default=False,
        expected_strict=False,
        description="algebraic self-cancel src - src does not create structural COI",
    ),
    VulnCaseSpec(
        "M4",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.DOCUMENTED,
        "vuln_top.u_m4.src",
        "vuln_top.u_m4.dst",
        expected_default=True,
        expected_strict=True,
        description="reduction OR |src — structural dependence on src",
    ),
    VulnCaseSpec(
        "M7",
        VulnGroup.SIGNAL_GRANULARITY,
        Remediation.DOCUMENTED,
        "vuln_top.u_m7.src",
        "vuln_top.u_m7.dst",
        expected_default=True,
        expected_strict=True,
        description="replicate select {2{src}}[1] is in-bounds and uses src",
    ),
    VulnCaseSpec(
        "M9",
        VulnGroup.CROSS_SCOPE_EXPR,
        Remediation.FIXED,
        "vuln_top.u_m9.src",
        "vuln_top.u_m9.dst",
        expected_default=False,
        expected_strict=False,
        description="system function $bits(src) excluded from structural COI",
    ),
    VulnCaseSpec(
        "N9",
        VulnGroup.UNRESOLVED_GEN,
        Remediation.FIXED,
        "vuln_top.u_n9.src",
        "vuln_top.u_n9.dst",
        expected_default=True,
        expected_strict=True,
        description="generate for with nested if(gi>0) unrolls chained assigns",
    ),
)


def plan_by_group() -> dict[VulnGroup, List[VulnCaseSpec]]:
    out: dict[VulnGroup, List[VulnCaseSpec]] = {}
    for spec in VULN_PLAN:
        out.setdefault(spec.group, []).append(spec)
    return out


def remediation_summary() -> List[str]:
    lines = ["Vulnerability remediation plan (grouped):"]
    for group, cases in plan_by_group().items():
        fixes = {c.remediation for c in cases}
        lines.append(
            f"  [{group.value}] {len(cases)} case(s) — remediation: "
            + ", ".join(sorted(s.value for s in fixes))
        )
        for c in cases:
            lines.append(f"      {c.case_id}: {c.description}")
    return lines