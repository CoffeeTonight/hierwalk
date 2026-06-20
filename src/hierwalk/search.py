"""Instance / module name search over elaborated hierarchy."""

from __future__ import annotations

import fnmatch
import re
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Union

PatternKind = Literal["auto", "instance", "path"]

SearchPatterns = Union[str, Sequence[str]]

from hierwalk.models import ElabNode, FlatRow, SearchHit


def parse_search_patterns(raw: str) -> List[str]:
    """Split ``niu,sramc`` or ``\"niu\",\"sramc\"`` into separate patterns."""
    out: List[str] = []
    for chunk in raw.split(","):
        token = chunk.strip().strip('"').strip("'")
        if token:
            out.append(token)
    return out


def normalize_search_patterns(pattern: SearchPatterns) -> List[str]:
    if isinstance(pattern, str):
        text = pattern.strip()
        if not text:
            return []
        if "," in text:
            return parse_search_patterns(text)
        return [text]
    return [p.strip() for p in pattern if str(p).strip()]


def _glob_to_regex(pattern: str) -> str:
    parts: List[str] = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _glob_match(text: str, pattern: str, *, case_insensitive: bool) -> bool:
    if case_insensitive:
        return (
            re.fullmatch(fnmatch.translate(pattern), text, re.IGNORECASE)
            is not None
        )
    return fnmatch.fnmatchcase(text, pattern)


_SEGMENT_REGEX_CACHE: Dict[tuple[str, bool], re.Pattern[str]] = {}


def _segment_uses_regex(pattern: str) -> bool:
    if pattern.startswith("re:"):
        return True
    return any(ch in pattern for ch in "+(){}|^$\\")


def _compile_segment_regex(
    pattern: str,
    *,
    case_insensitive: bool,
) -> re.Pattern[str]:
    raw = pattern[3:] if pattern.startswith("re:") else pattern
    key = (raw, case_insensitive)
    compiled = _SEGMENT_REGEX_CACHE.get(key)
    if compiled is None:
        flags = re.IGNORECASE if case_insensitive else 0
        compiled = re.compile(raw, flags)
        _SEGMENT_REGEX_CACHE[key] = compiled
    return compiled


def _regex_segment_match(
    segment: str,
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> bool:
    return (
        _compile_segment_regex(pattern, case_insensitive=case_insensitive).fullmatch(
            segment
        )
        is not None
    )


def _segment_glob_match(
    segment: str,
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> bool:
    if any(ch in pattern for ch in "*?[]"):
        if _glob_match(segment, pattern, case_insensitive=case_insensitive):
            return True
        flags = re.IGNORECASE if case_insensitive else 0
        return re.search(_glob_to_regex(pattern), segment, flags) is not None
    if case_insensitive:
        return segment.lower() == pattern.lower()
    return segment == pattern


def _segment_match(
    segment: str,
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> bool:
    if _segment_uses_regex(pattern):
        return _regex_segment_match(
            segment,
            pattern,
            case_insensitive=case_insensitive,
        )
    return _segment_glob_match(
        segment,
        pattern,
        case_insensitive=case_insensitive,
    )


def _name_match(name: str, pattern: str, *, case_insensitive: bool = False) -> bool:
    if _segment_uses_regex(pattern):
        return _regex_segment_match(
            name,
            pattern,
            case_insensitive=case_insensitive,
        )
    if any(ch in pattern for ch in "*?[]"):
        return _glob_match(name, pattern, case_insensitive=case_insensitive)
    if any(ch in pattern for ch in ".^$+?{}|()\\"):
        flags = re.IGNORECASE if case_insensitive else 0
        return re.compile(pattern, flags).search(name) is not None
    if case_insensitive:
        return pattern.lower() in name.lower()
    return pattern.lower() in name.lower()


def _uses_path_pattern(pattern: str) -> bool:
    return "." in pattern


_PATH_ELLIPSIS = ".."


def _tokenize_path_pattern(pattern: str) -> List[str]:
    """Split on ``.``; consecutive dots form a single ``..`` ellipsis token."""
    tokens: List[str] = []
    i = 0
    acc: List[str] = []

    def flush() -> None:
        if acc:
            tokens.append("".join(acc))
            acc.clear()

    while i < len(pattern):
        ch = pattern[i]
        if ch == ".":
            if i + 1 < len(pattern) and pattern[i + 1] == ".":
                flush()
                tokens.append(_PATH_ELLIPSIS)
                i += 2
                while i < len(pattern) and pattern[i] == ".":
                    i += 1
            else:
                flush()
                i += 1
        else:
            acc.append(ch)
            i += 1
    flush()
    return tokens


def _match_path_tokens(
    path_parts: Sequence[str],
    tokens: Sequence[str],
    *,
    case_insensitive: bool = False,
    pi: int = 0,
    ti: int = 0,
) -> bool:
    if ti >= len(tokens):
        return pi == len(path_parts)
    tok = tokens[ti]
    if tok == _PATH_ELLIPSIS:
        if ti == len(tokens) - 1:
            return pi < len(path_parts)
        for skip in range(1, len(path_parts) - pi + 1):
            if _match_path_tokens(
                path_parts,
                tokens,
                case_insensitive=case_insensitive,
                pi=pi + skip,
                ti=ti + 1,
            ):
                return True
        return False
    if pi >= len(path_parts):
        return False
    if not _segment_match(
        path_parts[pi],
        tok,
        case_insensitive=case_insensitive,
    ):
        return False
    return _match_path_tokens(
        path_parts,
        tokens,
        case_insensitive=case_insensitive,
        pi=pi + 1,
        ti=ti + 1,
    )


def path_pattern_match(
    full_path: str,
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> bool:
    """
    Match hierarchy paths.

    - ``*niu*`` (no dots) — any single path segment matches the glob.
    - ``a.b.*c`` — exactly three segments from root; ``*`` only spans node names.
    - ``top.a..*c`` — ``..`` crosses one or more intermediate segments.
    - ``er_[0-9]+[xyz]`` — regex segment when ``+ ( ) { } | ^ $ \\`` appear;
      optional ``re:`` prefix; compiled once per pattern string.
    """
    if not pattern:
        return False
    tokens = _tokenize_path_pattern(pattern)
    if not tokens:
        return False
    path_parts = full_path.split(".")
    if len(tokens) == 1 and tokens[0] != _PATH_ELLIPSIS:
        return any(
            _segment_match(
                seg,
                tokens[0],
                case_insensitive=case_insensitive,
            )
            for seg in path_parts
        )
    return _match_path_tokens(
        path_parts,
        tokens,
        case_insensitive=case_insensitive,
    )


def _instance_search_name(row: FlatRow) -> str:
    """Instance token for name/glob search (escaped ids keep embedded dots).

    Elaboration normally supplies a single hierarchy segment in ``inst_leaf``.
    Unescaped dotted leaves (e.g. generate labels) match on the trailing segment.
    """
    leaf = row.inst_leaf
    if leaf.startswith("\\"):
        return leaf
    if "." in leaf:
        return leaf.rsplit(".", 1)[-1]
    return leaf


def row_matches_search_pattern(
    row: FlatRow,
    pattern: str,
    *,
    match_inst: bool,
    match_module: bool,
    pattern_kind: PatternKind = "auto",
    case_insensitive: bool = False,
) -> bool:
    if match_inst:
        if pattern_kind in ("auto", "path") and (
            pattern_kind == "path" or _uses_path_pattern(pattern)
        ):
            if path_pattern_match(
                row.full_path,
                pattern,
                case_insensitive=case_insensitive,
            ):
                return True
        if pattern_kind in ("auto", "instance") and not (
            pattern_kind == "auto" and _uses_path_pattern(pattern)
        ):
            if _name_match(
                _instance_search_name(row),
                pattern,
                case_insensitive=case_insensitive,
            ):
                return True
    if match_module and _name_match(
        row.module,
        pattern,
        case_insensitive=case_insensitive,
    ):
        return True
    return False


def _under_any_prefix(path: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        if path == prefix or path.startswith(f"{prefix}."):
            return True
    return False


def hit_from_row(
    row: FlatRow,
    *,
    matched_name: str,
    match_kind: str,
    full_path: Optional[str] = None,
) -> SearchHit:
    return SearchHit(
        full_path=full_path or row.full_path,
        matched_name=matched_name,
        module=row.module,
        depth=row.depth,
        file=row.file,
        match_kind=match_kind,
        stop_reason=row.stop_reason,
        via_filelist=row.via_filelist,
        filelist_chain=row.filelist_chain,
    )


def search_flat_rows(
    rows: Sequence[FlatRow],
    pattern: SearchPatterns,
    *,
    match_inst: bool = True,
    match_module: bool = False,
    include_subtree: bool = False,
    pattern_kind: PatternKind = "auto",
    case_insensitive: bool = False,
) -> List[SearchHit]:
    """
    Search flattened instance rows.

    Each :class:`FlatRow` already carries ``full_path`` (top→leaf). Multiple
    patterns (comma-separated string or sequence) are combined with OR. With
    ``include_subtree``, anchors are instance rows whose ``inst_leaf`` (or
    module type) matches any pattern, then every descendant row under those
    anchors is included.
    """
    patterns = normalize_search_patterns(pattern)
    anchors: set[str] = set()
    anchor_kinds: Dict[str, str] = {}
    for row in rows:
        for pat in patterns:
            if row_matches_search_pattern(
                row,
                pat,
                match_inst=match_inst,
                match_module=match_module,
                pattern_kind=pattern_kind,
                case_insensitive=case_insensitive,
            ):
                anchors.add(row.full_path)
                if match_module and _name_match(
                    row.module,
                    pat,
                    case_insensitive=case_insensitive,
                ):
                    anchor_kinds[row.full_path] = "module"
                else:
                    anchor_kinds[row.full_path] = "instance"
                break

    if not anchors:
        return []

    if not include_subtree:
        hits: List[SearchHit] = []
        for row in rows:
            if row.full_path not in anchors:
                continue
            kind = anchor_kinds[row.full_path]
            matched = row.inst_leaf if kind == "instance" else row.module
            hits.append(hit_from_row(row, matched_name=matched, match_kind=kind))
        hits.sort(key=lambda h: h.full_path)
        return hits

    hits = []
    for row in rows:
        if not _under_any_prefix(row.full_path, sorted(anchors)):
            continue
        if row.full_path in anchors:
            kind = anchor_kinds[row.full_path]
            matched = row.inst_leaf if kind == "instance" else row.module
            hits.append(hit_from_row(row, matched_name=matched, match_kind=kind))
        else:
            hits.append(
                hit_from_row(
                    row,
                    matched_name=row.inst_leaf,
                    match_kind="hierarchy-under",
                )
            )
    hits.sort(key=lambda h: h.full_path)
    return hits


def enrich_hits_from_rows(hits: Sequence[SearchHit], rows: Sequence[FlatRow]) -> List[SearchHit]:
    """Attach filelist provenance using instance path (strip port suffix if any)."""
    by_path = {row.full_path: row for row in rows}
    out: List[SearchHit] = []
    for hit in hits:
        inst_path = hit.full_path
        if hit.port_name and inst_path.endswith(f".{hit.port_name}"):
            inst_path = inst_path[: -(len(hit.port_name) + 1)]
        row = by_path.get(inst_path)
        if row is None:
            out.append(hit)
            continue
        out.append(
            SearchHit(
                full_path=hit.full_path,
                matched_name=hit.matched_name,
                module=hit.module,
                depth=hit.depth,
                file=hit.file or row.file,
                match_kind=hit.match_kind,
                stop_reason=hit.stop_reason or row.stop_reason,
                via_filelist=row.via_filelist,
                filelist_chain=row.filelist_chain,
                port_name=hit.port_name,
                port_found=hit.port_found,
                port_line=hit.port_line,
                port_decl=hit.port_decl,
                port_param_note=hit.port_param_note,
            )
        )
    return out


def search_tree(
    root: ElabNode,
    pattern: str,
    *,
    match_inst: bool = True,
    match_module: bool = False,
    case_insensitive: bool = False,
) -> List[SearchHit]:
    hits: List[SearchHit] = []

    def walk(node: ElabNode) -> None:
        if match_inst and _name_match(
            node.inst_name,
            pattern,
            case_insensitive=case_insensitive,
        ):
            hits.append(
                SearchHit(
                    full_path=node.full_path,
                    matched_name=node.inst_name,
                    module=node.module,
                    depth=node.full_path.count("."),
                    file=node.file_path,
                    match_kind="instance",
                    stop_reason=node.stop_reason,
                )
            )
        if match_module and _name_match(
            node.module,
            pattern,
            case_insensitive=case_insensitive,
        ):
            hits.append(
                SearchHit(
                    full_path=node.full_path,
                    matched_name=node.module,
                    module=node.module,
                    depth=node.full_path.count("."),
                    file=node.file_path,
                    match_kind="module",
                    stop_reason=node.stop_reason,
                )
            )
        for child in node.children:
            walk(child)

    walk(root)
    return hits


def search(
    pattern: SearchPatterns,
    *,
    rows: Optional[Sequence[FlatRow]] = None,
    root: Optional[ElabNode] = None,
    match_inst: bool = True,
    match_module: bool = False,
    include_subtree: bool = False,
    pattern_kind: PatternKind = "auto",
    case_insensitive: bool = False,
) -> List[SearchHit]:
    patterns = normalize_search_patterns(pattern)
    if rows is not None:
        return search_flat_rows(
            rows,
            patterns,
            match_inst=match_inst,
            match_module=match_module,
            include_subtree=include_subtree,
            pattern_kind=pattern_kind,
            case_insensitive=case_insensitive,
        )
    if root is not None:
        hits: List[SearchHit] = []
        seen: set[str] = set()
        for pat in patterns:
            for hit in search_tree(
                root,
                pat,
                match_inst=match_inst,
                match_module=match_module,
                case_insensitive=case_insensitive,
            ):
                if hit.full_path in seen:
                    continue
                seen.add(hit.full_path)
                hits.append(hit)
        hits.sort(key=lambda h: h.full_path)
        return hits
    return []