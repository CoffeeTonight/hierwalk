"""
Bit-slice awareness for connectivity seeds and results.

WARNING
-------
Different bit selects on the same base net are NOT the same electrical
endpoint for bit-accurate claims. Coalescing must be explicit:

* ``base`` mode — meet on (hier, base_name); many slices may share one expand
  point; results MUST be labeled ``connectivity_level="base"`` (structure only).
* ``strict_slice`` mode — seeds with different normalized selects never share
  a coalesce key; pairs keep per-slice identity. Still not a formal bit proof
  unless edge meta maps ranges (future).

Never silently treat whole-net and partial-select, or non-overlapping ranges,
as identical without labeling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_LIT_SEL = re.compile(r"^\s*(\d+)\s*(?::\s*(\d+)\s*)?$")
_PATH_SEL = re.compile(r"^([A-Za-z_]\w*)((?:\[[^\]]+\])*)$")

# Connectivity levels reported on pairs / checks
LEVEL_BASE = "base"  # structural base-name meet; slice not proven
LEVEL_SLICE_ID = "slice_identity"  # same normalized sel on seeds; still not map-proof
LEVEL_SLICE_HINT = "slice_hint"  # edge meta has literal sels that look consistent
LEVEL_APPROX = "select_approx"  # non-literal / param / genvar select involved


@dataclass(frozen=True)
class BitRange:
    """Inclusive bit range; always stored hi >= lo."""

    hi: int
    lo: int

    @staticmethod
    def from_pair(a: int, b: int) -> "BitRange":
        return BitRange(hi=max(a, b), lo=min(a, b))

    def fmt(self) -> str:
        if self.hi == self.lo:
            return f"[{self.hi}]"
        return f"[{self.hi}:{self.lo}]"

    def overlaps(self, other: "BitRange") -> bool:
        return not (self.lo > other.hi or other.lo > self.hi)

    def contains(self, other: "BitRange") -> bool:
        return self.lo <= other.lo and self.hi >= other.hi


def parse_sel_string(sel: Optional[str]) -> Tuple[Optional[BitRange], bool]:
    """
    Parse ``[3]`` / ``[7:4]`` → (BitRange, approx).
    approx=True if brackets present but not integer literals.
    """
    if not sel or not str(sel).strip():
        return None, False
    s = str(sel).strip()
    parts = re.findall(r"\[([^\]]*)\]", s)
    if not parts:
        # bare "3" or "7:4"
        m = _LIT_SEL.match(s)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) is not None else a
            return BitRange.from_pair(a, b), False
        return None, True
    if len(parts) != 1:
        return None, True
    m = _LIT_SEL.match(parts[0])
    if not m:
        return None, True
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) is not None else a
    return BitRange.from_pair(a, b), False


def normalize_sel(sel: Optional[str]) -> Tuple[Optional[str], bool]:
    """Canonical sel string or (None, approx). [4:7] → [7:4] (hi:lo)."""
    br, approx = parse_sel_string(sel)
    if approx:
        return None, True
    if br is None:
        return None, False
    return br.fmt(), False


def parse_hierarchy_seed(path: str) -> Tuple[str, str, Optional[str], bool]:
    """
    ``top.u.sig`` / ``top.u.sig[3:0]`` →
    (instance_hier, base_name, sel_norm|None, select_approx).
    """
    path = path.strip()
    parts = path.split(".")
    if not parts:
        return "", "", None, False
    tail = parts[-1]
    m = _PATH_SEL.match(tail.strip())
    if not m:
        base = tail.split("[", 1)[0]
        hier = ".".join(parts[:-1]) if len(parts) > 1 else ""
        return hier, base, None, False
    base, brackets = m.group(1), m.group(2) or ""
    hier = ".".join(parts[:-1]) if len(parts) > 1 else ""
    if not brackets:
        return hier, base, None, False
    # full bracket string
    sel_raw = brackets if brackets.startswith("[") else f"[{brackets}]"
    # multi-dim
    if brackets.count("[") > 1:
        return hier, base, None, True
    norm, approx = normalize_sel(sel_raw)
    return hier, base, norm, approx


@dataclass
class SeedRec:
    """One hierarchy string as a connectivity seed."""

    path: str
    hier: str  # instance path (no signal)
    base: str
    sel: Optional[str]  # normalized literal or None
    select_approx: bool = False

    @property
    def net_key_base(self) -> Tuple[str, str]:
        return (self.hier, self.base)

    @property
    def coalesce_key_strict(self) -> Tuple[str, str, str]:
        """Strict: different sels never merge. approx → unique by path."""
        if self.select_approx:
            return (self.hier, self.base, f"approx:{self.path}")
        return (self.hier, self.base, self.sel or "")

    @property
    def coalesce_key_base(self) -> Tuple[str, str]:
        """Base mode: same hier+base share expand point (slice kept in meta only)."""
        return (self.hier, self.base)


def seeds_from_paths(paths: Sequence[str]) -> List[SeedRec]:
    out: List[SeedRec] = []
    for p in paths:
        p = str(p).strip()
        if not p:
            continue
        hier, base, sel, approx = parse_hierarchy_seed(p)
        out.append(
            SeedRec(path=p, hier=hier, base=base, sel=sel, select_approx=approx)
        )
    return out


@dataclass
class CoalescedSeed:
    """One expand point after coalesce; may carry many original paths."""

    hier: str
    base: str
    # normalized sel if ALL members share the same literal sel; else None
    common_sel: Optional[str]
    # True if any member has approx select or mixed sels / whole+part mix
    mixed_or_approx: bool
    members: List[SeedRec] = field(default_factory=list)
    # expand uses base NetKey always for graph (structure)
    # labels = member paths

    @property
    def net_key(self) -> Tuple[str, str]:
        return (self.hier, self.base)

    @property
    def paths(self) -> List[str]:
        return [m.path for m in self.members]

    def connectivity_level_for_expand(self) -> str:
        if self.mixed_or_approx:
            return LEVEL_APPROX if any(m.select_approx for m in self.members) else LEVEL_BASE
        if self.common_sel is not None:
            return LEVEL_SLICE_ID
        # all whole-net
        return LEVEL_BASE


def coalesce_seeds(
    seeds: Sequence[SeedRec],
    *,
    mode: str = "strict_slice",
) -> List[CoalescedSeed]:
    """
    Coalesce seeds for expand.

    * ``strict_slice`` (default, safe): only merge identical
      (hier, base, normalized_sel). Different slices stay separate labels
      but still use base NetKey for graph walk — **labels do not cross-merge**.
      Wait: if they use same NetKey for BFS, labels OR-merge on visit...

    Important BFS interaction
    -------------------------
    bi-meet stores labels (paths) on NetKey=(hier,base). If two seeds with
    different sels share NetKey, **both paths get the meet** even when only
    one bit is connected. That is the bug to avoid.

    Therefore in ``strict_slice`` we keep separate CoalescedSeed entries and
    run meet with **label isolation**: each coalesced group is a separate
    expand with only its own paths as labels (multiple meet runs) OR we use
    a label tag that includes sel.

    Implementation choice: **strict_slice** → each CoalescedSeed is expanded
    with only its member paths as labels (caller runs one bi_meet per pair of
    groups, but seeds within a side that differ by slice are NOT OR-merged
    onto the same key with different paths... actually they still share NetKey.

    Fix: use Label = path (unique). On first visit to NetKey, only attach
    labels from the seed that started this frontier component...

    Simpler fix used here:
    - ``strict_slice``: coalesce only same (hier, base, sel); for bi_meet,
      pass seeds so that lab_a[NetKey] only gets paths that share the same
      sel class. When adding seed, key the label set by (NetKey, sel_class).

    We change bi_meet to accept SeedAttach with optional sel_class and store
    labels in lab as path → but meet pairs only paths whose sel_class is
    compatible... 

    Simplest correct approach for strict:
    **Run expand labels only among paths in the same CoalescedSeed.**
    When multiple CoalescedSeeds share NetKey (different sel), they still
    share the graph node but we use:
      lab_a: Dict[NetKey, Dict[sel_class, Set[path]]]
    Meet only pairs paths from sel_classes that are allowed:
      - same sel_class
      - or either is whole-net "" with explicit base mode

    For this module, coalesce returns groups; bi_meet is updated separately.
    """
    mode = (mode or "strict_slice").strip().lower()
    buckets: Dict[Any, List[SeedRec]] = {}
    order: List[Any] = []

    for s in seeds:
        if mode == "base":
            # WARNING: structural only — caller must label results as base
            k: Any = s.coalesce_key_base
        else:
            # strict_slice
            k = s.coalesce_key_strict
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(s)

    out: List[CoalescedSeed] = []
    for k in order:
        members = buckets[k]
        sels = {m.sel for m in members}
        approx = any(m.select_approx for m in members)
        # whole + partial in same base bucket only happens in mode=base
        has_whole = any(m.sel is None and not m.select_approx for m in members)
        has_part = any(m.sel is not None for m in members)
        mixed = approx or (has_whole and has_part) or (len(sels - {None}) > 1)
        common: Optional[str] = None
        if not mixed and len(sels) == 1:
            common = next(iter(sels))
        elif not mixed and len(sels) == 1:
            common = members[0].sel
        out.append(
            CoalescedSeed(
                hier=members[0].hier,
                base=members[0].base,
                common_sel=common if not mixed else None,
                mixed_or_approx=mixed or approx,
                members=list(members),
            )
        )
    return out


def sel_class(seed: SeedRec) -> str:
    """Label class for BFS isolation."""
    if seed.select_approx:
        return f"approx:{seed.path}"
    if seed.sel is None:
        return ""  # whole net
    return seed.sel


def ranges_compatible_for_pair(
    src_sel: Optional[str],
    dst_sel: Optional[str],
    *,
    allow_base: bool = True,
) -> Tuple[bool, str]:
    """
    Whether reporting a structural pair is OK for these seed sels.

    Returns (ok, level).
    """
    if src_sel is None and dst_sel is None:
        return True, LEVEL_BASE
    # one whole, one slice → base only if allow_base
    if src_sel is None or dst_sel is None:
        if allow_base:
            return True, LEVEL_BASE
        return False, LEVEL_BASE
    sn, sa = normalize_sel(src_sel)
    dn, da = normalize_sel(dst_sel)
    if sa or da:
        return True, LEVEL_APPROX
    if sn == dn:
        return True, LEVEL_SLICE_ID
    # different literal ranges — do not treat as same bit endpoint
    br_s, _ = parse_sel_string(sn)
    br_d, _ = parse_sel_string(dn)
    if br_s and br_d and br_s.overlaps(br_d):
        # overlapping but not equal — base structural, not identity
        return True, LEVEL_BASE
    # non-overlapping: still might share base net structurally, but bit-distinct
    # Allow pair at base level with warning level
    if allow_base:
        return True, LEVEL_BASE
    return False, LEVEL_BASE


def annotate_pair_slice(
    src_path: str,
    dst_path: str,
    evidence: List[Dict[str, Any]],
    *,
    allow_base_meet: bool = True,
) -> Dict[str, Any]:
    """Build slice-related fields for a pair result."""
    sh, sb, ss, sa = parse_hierarchy_seed(src_path)
    dh, db, ds, da = parse_hierarchy_seed(dst_path)
    ok, level = ranges_compatible_for_pair(ss, ds, allow_base=allow_base_meet)

    # edge meta hints
    edge_has_lit = False
    edge_approx = False
    for ev in evidence or []:
        if ev.get("dst_sel") or ev.get("src_sels") or ev.get("src_sel"):
            edge_has_lit = True
        if ev.get("select_approx"):
            edge_approx = True
    if edge_approx:
        level = LEVEL_APPROX
    elif edge_has_lit and level == LEVEL_SLICE_ID:
        level = LEVEL_SLICE_HINT

    notes: List[str] = []
    if level == LEVEL_BASE and (ss or ds):
        notes.append(
            "structural meet on base net name; bit-slice mapping is NOT proven"
        )
    if ss and ds and ss != ds and level == LEVEL_BASE:
        notes.append(
            f"src_sel {ss} and dst_sel {ds} differ; do not treat as same bits"
        )
    if sa or da:
        notes.append("non-literal select on seed (param/genvar/approx)")
    if not ok:
        notes.append("pair suppressed by strict slice policy")

    return {
        "connectivity_level": level,
        "src_sel": ss,
        "dst_sel": ds,
        "src_select_approx": sa,
        "dst_select_approx": da,
        "slice_notes": notes,
        "pair_allowed": ok,
    }


def summarize_seed_group(seeds: Sequence[SeedRec]) -> Dict[str, Any]:
    """Stats for logging / JSON meta."""
    n = len(seeds)
    n_slice = sum(1 for s in seeds if s.sel is not None)
    n_approx = sum(1 for s in seeds if s.select_approx)
    n_whole = sum(1 for s in seeds if s.sel is None and not s.select_approx)
    bases = {(s.hier, s.base) for s in seeds}
    return {
        "n_paths": n,
        "n_whole_net": n_whole,
        "n_literal_slice": n_slice,
        "n_approx_slice": n_approx,
        "n_unique_base_nets": len(bases),
        "policy": (
            "Different bit selects on the same base are distinct endpoints "
            "for bit claims; base-level meet must be labeled connectivity_level=base."
        ),
    }
