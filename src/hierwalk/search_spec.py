"""Structured search query (instance / path / hierarchy_path) + case policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from hierwalk.index import DesignIndex
from hierwalk.models import FlatRow, SearchHit
from hierwalk.path_search import search_hierarchy_path
from hierwalk.search import normalize_search_patterns, search_flat_rows


def _ci_get(block: Mapping[str, Any], *keys: str) -> Any:
    lower = {str(k).lower().replace("-", "_"): v for k, v in block.items()}
    for key in keys:
        norm = key.lower().replace("-", "_")
        if norm in lower:
            return lower[norm]
    return None


def _document_has_key(block: Mapping[str, Any], *keys: str) -> bool:
    lower = {str(k).lower().replace("-", "_") for k in block}
    for key in keys:
        if key.lower().replace("-", "_") in lower:
            return True
    return False


@dataclass(frozen=True)
class SearchSpec:
    instance: Tuple[str, ...] = ()
    path: Tuple[str, ...] = ()
    hierarchy_path: Tuple[str, ...] = ()
    case_insensitive: bool = False
    search_module: bool = False
    search_subtree: bool = True

    def is_active(self) -> bool:
        return bool(self.instance or self.path or self.hierarchy_path)

    def summary_pattern(self) -> str:
        parts: List[str] = []
        if self.instance:
            parts.append("instance:" + ",".join(self.instance))
        if self.path:
            parts.append("path:" + ",".join(self.path))
        if self.hierarchy_path:
            parts.append("hierarchy_path:" + ",".join(self.hierarchy_path))
        return "; ".join(parts)


def _parse_pattern_list(value: Any, *, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return normalize_search_patterns(value)
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            out.extend(normalize_search_patterns(str(item)))
        return out
    raise ValueError(f"'{field}' must be a string or array of patterns")


def parse_search_spec_block(
    block: Mapping[str, Any],
    *,
    default_module: bool = False,
    default_subtree: bool = True,
) -> SearchSpec:
    instance = _parse_pattern_list(
        _ci_get(block, "instance"),
        field="search.instance",
    )
    path = _parse_pattern_list(
        _ci_get(block, "path"),
        field="search.path",
    )
    hierarchy_path = _parse_pattern_list(
        _ci_get(block, "hierarchy_path", "hierarchy-path"),
        field="search.hierarchy_path",
    )
    case_insensitive = bool(
        _ci_get(block, "case_insensitive", "case-insensitive") or False
    )
    if _document_has_key(block, "search_module", "search-module"):
        search_module = bool(_ci_get(block, "search_module", "search-module"))
    else:
        search_module = default_module
    if _document_has_key(block, "search_subtree", "search-subtree"):
        search_subtree = bool(_ci_get(block, "search_subtree", "search-subtree"))
    else:
        search_subtree = default_subtree
    return SearchSpec(
        instance=tuple(instance),
        path=tuple(path),
        hierarchy_path=tuple(hierarchy_path),
        case_insensitive=case_insensitive,
        search_module=search_module,
        search_subtree=search_subtree,
    )


def build_search_spec_from_legacy(
    *,
    search: Optional[str] = None,
    search_path: Optional[str] = None,
    search_module: bool = False,
    search_subtree: bool = True,
    case_insensitive: bool = False,
) -> Optional[SearchSpec]:
    instance: List[str] = []
    path: List[str] = []
    hierarchy_path: List[str] = []

    for pat in normalize_search_patterns(search or ""):
        if "." in pat:
            path.append(pat)
        else:
            instance.append(pat)

    hierarchy_path.extend(normalize_search_patterns(search_path or ""))

    spec = SearchSpec(
        instance=tuple(instance),
        path=tuple(path),
        hierarchy_path=tuple(hierarchy_path),
        case_insensitive=case_insensitive,
        search_module=search_module,
        search_subtree=search_subtree,
    )
    return spec if spec.is_active() else None


def resolve_search_spec(
    data: Mapping[str, Any],
    *,
    case_insensitive: bool = False,
) -> Optional[SearchSpec]:
    """Build :class:`SearchSpec` from JSON (structured block or legacy flat keys)."""
    default_module = bool(
        _ci_get(data, "search_module", "search-module") or False
    )
    if _document_has_key(data, "search_subtree", "search-subtree"):
        default_subtree = bool(
            _ci_get(data, "search_subtree", "search-subtree")
        )
    else:
        default_subtree = True
    raw_search = _ci_get(data, "search")
    if isinstance(raw_search, Mapping):
        spec = parse_search_spec_block(
            raw_search,
            default_module=default_module,
            default_subtree=default_subtree,
        )
    else:
        spec = build_search_spec_from_legacy(
            search=str(raw_search or "").strip() or None,
            search_path=str(
                _ci_get(data, "search_path", "search-path") or ""
            ).strip()
            or None,
            search_module=default_module,
            search_subtree=default_subtree,
            case_insensitive=case_insensitive
            or bool(
                _ci_get(data, "search_case_insensitive", "search-case-insensitive")
            ),
        )
        if spec is None:
            return None
        return spec

    if case_insensitive or bool(
        _ci_get(data, "search_case_insensitive", "search-case-insensitive")
    ):
        spec = replace(spec, case_insensitive=True)
    if not spec.search_module and default_module:
        spec = replace(spec, search_module=True)
    if not spec.search_subtree and default_subtree:
        spec = replace(spec, search_subtree=True)

    extra_hierarchy = str(
        _ci_get(data, "search_path", "search-path") or ""
    ).strip()
    if extra_hierarchy and isinstance(raw_search, Mapping):
        merged = tuple(
            dict.fromkeys(
                list(spec.hierarchy_path)
                + normalize_search_patterns(extra_hierarchy)
            )
        )
        spec = replace(spec, hierarchy_path=merged)

    return spec if spec.is_active() else None


def dedupe_search_hits(hits: Sequence[SearchHit]) -> List[SearchHit]:
    seen: set[str] = set()
    out: List[SearchHit] = []
    for hit in sorted(hits, key=lambda h: h.full_path):
        if hit.full_path in seen:
            continue
        seen.add(hit.full_path)
        out.append(hit)
    return out


def effective_search_spec(cfg: Any) -> Optional[SearchSpec]:
    """Resolve structured or legacy flat search fields on :class:`RunConfig`."""
    if getattr(cfg, "search_spec", None) is not None:
        spec: Optional[SearchSpec] = cfg.search_spec
    else:
        spec = build_search_spec_from_legacy(
            search=getattr(cfg, "search", None),
            search_path=getattr(cfg, "search_path", None),
            search_module=bool(getattr(cfg, "search_module", False)),
            search_subtree=bool(getattr(cfg, "search_subtree", True)),
            case_insensitive=bool(getattr(cfg, "search_case_insensitive", False)),
        )
    if spec is None:
        return None
    if bool(getattr(cfg, "search_case_insensitive", False)):
        spec = replace(spec, case_insensitive=True)
    return spec if spec.is_active() else None


def document_has_search(data: Mapping[str, Any]) -> bool:
    return resolve_search_spec(data) is not None


def execute_search_spec(
    rows: Sequence[FlatRow],
    index: DesignIndex,
    spec: SearchSpec,
) -> List[SearchHit]:
    hits: List[SearchHit] = []
    if spec.instance:
        hits.extend(
            search_flat_rows(
                rows,
                list(spec.instance),
                match_inst=True,
                match_module=spec.search_module,
                include_subtree=spec.search_subtree,
                pattern_kind="instance",
                case_insensitive=spec.case_insensitive,
            )
        )
    if spec.path:
        hits.extend(
            search_flat_rows(
                rows,
                list(spec.path),
                match_inst=True,
                match_module=spec.search_module,
                include_subtree=spec.search_subtree,
                pattern_kind="path",
                case_insensitive=spec.case_insensitive,
            )
        )
    for pattern in spec.hierarchy_path:
        hits.extend(
            search_hierarchy_path(
                rows,
                pattern,
                index,
                case_insensitive=spec.case_insensitive,
            )
        )
    return dedupe_search_hits(hits)