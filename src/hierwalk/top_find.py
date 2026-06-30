"""Infer top module candidates from the module index."""

from __future__ import annotations

from typing import List, Sequence

from hierwalk.ignore_path import source_path_matches
from hierwalk.index import DesignIndex


def _is_top_candidate(index: DesignIndex, name: str) -> bool:
    rec = index.get_module(name)
    if not rec:
        return False
    if rec.stop_reason == "ignorePath" or rec.is_blackbox:
        return False
    if source_path_matches(rec.file_path, index.ignore_path_patterns):
        return False
    if index.module_stop_reason(name) == "ignorePath":
        return False
    return True


def find_top_modules(index: DesignIndex) -> List[str]:
    """
    Modules never instantiated as a child, excluding ignorePath/blackbox stubs.

    Matches hc_hierarchy / regexVerilogAST intent but skips IP/orphan stubs
    created by ``--ignore-path``.
    """
    instantiated: set[str] = set()
    for rec in index.modules.values():
        if rec.needs_generate_fold:
            edges = index.instances_for(rec.module_name, {}, {})
        else:
            edges = rec.instances
        for edge in edges:
            instantiated.add(edge.child_module)

    candidates = [
        name
        for name in sorted(index.modules)
        if name not in instantiated and _is_top_candidate(index, name)
    ]
    if candidates:
        return candidates
    return [
        name
        for name in sorted(index.modules)
        if _is_top_candidate(index, name)
    ]


def resolve_top_modules(
    index: DesignIndex,
    *,
    top: str | None,
    filelist_tops: Sequence[str] = (),
    all_tops: bool = False,
) -> List[str]:
    """Pick root module(s) for elaboration."""
    if top:
        if top not in index.modules:
            hinted = [t for t in filelist_tops if t]
            extra = f"; filelist -top hints: {', '.join(hinted)}" if hinted else ""
            raise ValueError(
                f"Top module not found: {top} "
                f"(index has {len(index.modules)} module(s)){extra}"
            )
        return [top]

    hinted = [t for t in filelist_tops if t in index.modules and _is_top_candidate(index, t)]
    if len(hinted) == 1 and not all_tops:
        return hinted
    if hinted and all_tops:
        return sorted(set(hinted))

    found = find_top_modules(index)
    if all_tops:
        return found
    if len(found) == 1:
        return found
    if len(hinted) > 1:
        raise ValueError(
            "Multiple -top hints in filelist: "
            + ", ".join(hinted)
            + " (use --top NAME)"
        )
    if len(found) > 1:
        raise ValueError(
            "Multiple top candidates: "
            + ", ".join(found)
            + " (use --top NAME or --find-top)"
        )
    if len(found) == 0:
        hinted_names = [t for t in filelist_tops if t]
        extra = ""
        if hinted_names:
            extra = f" (filelist -top hints: {', '.join(hinted_names)})"
        raise ValueError(
            "No top module candidate in index"
            + extra
            + " — set JSON top, -top in .f, or use --top NAME"
        )
    return found