"""Verilog preprocessor: comments, include, define/undef, ifdef, macro expand."""

from __future__ import annotations

import os
import re
import sys
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from hierwalk.ignore_path import source_path_matches
from hierwalk.progress import format_work_location, maybe_track_work

_IGNORE_PATH_STUB = "/* hierwalk: ignore-path skipped */"

_IFDEF_RE = re.compile(
    r"`(?:ifdef|ifndef)\s+([A-Za-z_]\w*)"
    r"|`elsif\s+([A-Za-z_]\w*)"
    r"|`(?:else|endif|end)\b",
    re.IGNORECASE,
)
_DEFINE_LINE_RE = re.compile(
    r"^\s*`define\s+([A-Za-z_]\w*)(?:\s+(.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
_DEFINE_INLINE_RE = re.compile(
    r"`define\s+([A-Za-z_]\w*)(?:\s+([^`\n]*))?",
    re.IGNORECASE,
)
_UNDEF_INLINE_RE = re.compile(
    r"`undef\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)
_UNDEF_LINE_RE = re.compile(
    r"^\s*`undef\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_INCLUDE_RE = re.compile(
    r"`include\s+([<\"])([^\">]+)[\">]",
    re.IGNORECASE,
)
_MACRO_USE_RE = re.compile(r"`([A-Za-z_]\w*)")
_BIND_LINE_RE = re.compile(r"^\s*bind\b", re.IGNORECASE | re.MULTILINE)
_INCLUDE_LINE_RE = re.compile(r"^\s*`include\b", re.IGNORECASE)
_INCLUDE_LINE_PARSE_RE = re.compile(
    r"^\s*`include\s+([<\"])([^\">]+)[\">]\s*$",
    re.IGNORECASE,
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_ENDIF_LABEL_COMMENT_RE = re.compile(
    r"^(\s*`(?:endif|else|end))\s*//\s*[A-Za-z_]\w*\s*(.*)$",
    re.IGNORECASE,
)

# Per-process include unit cache: (path, mtime_ns, size, skip_patterns) -> expanded text.
_IncludeCacheKey = Tuple[str, int, int, Tuple[str, ...]]
_DefineOp = Tuple[str, str, str]  # ("set"|"undef", name, value)
_INCLUDE_UNIT_CACHE: Dict[_IncludeCacheKey, str] = {}

# Per-process source translation-unit cache after full preprocess.
_SourcePreprocessKey = Tuple[str, int, int, str, Tuple[Tuple[str, str], ...], Tuple[str, ...]]
_SourcePreprocessEntry = Tuple[str, Tuple[_DefineOp, ...]]
_SOURCE_PREPROCESS_CACHE: Dict[_SourcePreprocessKey, _SourcePreprocessEntry] = {}


def clear_include_unit_cache() -> None:
    """Drop cached include expansions (tests / long-lived workers)."""
    _INCLUDE_UNIT_CACHE.clear()
    _SOURCE_PREPROCESS_CACHE.clear()


def _snapshot_include_cache() -> Dict[_IncludeCacheKey, str]:
    return dict(_INCLUDE_UNIT_CACHE)


def _snapshot_source_preprocess_cache() -> Dict[_SourcePreprocessKey, _SourcePreprocessEntry]:
    return dict(_SOURCE_PREPROCESS_CACHE)


def _defines_delta(
    base: Mapping[str, str],
    final: Mapping[str, str],
) -> Tuple[_DefineOp, ...]:
    """Ops to turn *base* into *final* (for preprocess cache replay)."""
    ops: List[_DefineOp] = []
    for name, val in final.items():
        if base.get(name) != val:
            ops.append(("set", name, val))
    for name in base:
        if name not in final:
            ops.append(("undef", name, ""))
    return tuple(ops)


def _restore_source_preprocess_cache_hit(
    defines: MutableMapping[str, str],
    entry: _SourcePreprocessEntry,
) -> str:
    text, ops = entry
    _apply_define_ops(defines, ops)
    return text


def _install_include_cache_snapshot(
    snapshot: Dict[_IncludeCacheKey, str],
) -> None:
    """Seed worker-local include cache (required when start method is ``spawn``)."""
    _INCLUDE_UNIT_CACHE.clear()
    _INCLUDE_UNIT_CACHE.update(snapshot)


def _install_preprocess_caches(
    include_snapshot: Dict[_IncludeCacheKey, str],
    source_snapshot: Dict[_SourcePreprocessKey, _SourcePreprocessEntry],
) -> None:
    """Seed worker-local include + source preprocess caches."""
    _INCLUDE_UNIT_CACHE.clear()
    _INCLUDE_UNIT_CACHE.update(include_snapshot)
    _SOURCE_PREPROCESS_CACHE.clear()
    _SOURCE_PREPROCESS_CACHE.update(source_snapshot)


def _iter_text_lines(text: str) -> Iterator[str]:
    """Yield lines without ``str.splitlines()`` materializing a full list."""
    if not text:
        return
    start = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            yield text[start:i]
            start = i + 1
        elif ch == "\r":
            end = i
            if i + 1 < n and text[i + 1] == "\n":
                i += 1
            yield text[start:end]
            start = i + 1
        i += 1
    if start < n:
        yield text[start:]


def _source_preprocess_cache_key(
    path: Path,
    base_defines: Mapping[str, str],
    mode: str,
    skip_path_patterns: Sequence[str],
) -> Optional[_SourcePreprocessKey]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (
        str(path.resolve()),
        st.st_mtime_ns,
        st.st_size,
        mode,
        tuple(sorted(base_defines.items())),
        tuple(skip_path_patterns),
    )


def _strip_comments_stateful(text: str, *, preserve_endif_label: bool = False) -> str:
    """
    Remove ``//`` and ``/* */`` comments in one pass.

    Inside an active ``//`` line comment, ``/*`` is plain text (not a block opener).
    When *preserve_endif_label* is set, `` `endif//MACRO`` lines keep RTL after the
    label (see :func:`rtl_after_ifdef_label_comment`).
    """
    out: List[str] = []
    i, n = 0, len(text)
    state = "normal"

    def at_line_start(pos: int) -> bool:
        return pos == 0 or text[pos - 1] in "\r\n"

    def append_newlines_from(pos: int) -> int:
        cur = pos
        if cur < n and text[cur] == "\r":
            out.append("\r")
            cur += 1
        if cur < n and text[cur] == "\n":
            out.append("\n")
            cur += 1
        return cur

    while i < n:
        if state == "line":
            if text[i] in "\r\n":
                i = append_newlines_from(i)
                state = "normal"
            else:
                i += 1
            continue

        if state == "block":
            if i < n - 1 and text[i : i + 2] == "*/":
                i += 2
                state = "normal"
            else:
                i += 1
            continue

        if preserve_endif_label and at_line_start(i):
            line_end = i
            while line_end < n and text[line_end] not in "\r\n":
                line_end += 1
            line = text[i:line_end]
            trailing = rtl_after_ifdef_label_comment(line)
            if trailing:
                m = _ENDIF_LABEL_COMMENT_RE.match(line)
                if m is not None:
                    out.append(m.group(1))
                    if trailing:
                        out.append(" ")
                        out.append(trailing)
                    i = line_end
                    if i < n:
                        i = append_newlines_from(i)
                    continue

        if i < n - 1 and text[i : i + 2] == "//":
            i += 2
            state = "line"
            continue
        if i < n - 1 and text[i : i + 2] == "/*":
            if text.startswith(_IGNORE_PATH_STUB, i):
                out.append(_IGNORE_PATH_STUB)
                i += len(_IGNORE_PATH_STUB)
                continue
            i += 2
            state = "block"
            continue

        out.append(text[i])
        i += 1

    return "".join(out)


def strip_comments(text: str) -> str:
    return _strip_comments_stateful(text, preserve_endif_label=False)


def rtl_after_ifdef_label_comment(line: str) -> str:
    """
    RTL tokens after `` `endif//MACRO`` / `` `else//MACRO`` on the same source line.

    Common in RTL: the ``//MACRO`` suffix is a human label, but engineers sometimes
    place instance declarations on the same line after the label.
    """
    m = _ENDIF_LABEL_COMMENT_RE.match(line)
    if not m:
        return ""
    return m.group(2).strip()


def strip_line_for_ifdef_scan(line: str) -> str:
    """Like :func:`strip_comments` but keep RTL after `` `endif//label``."""
    trailing = rtl_after_ifdef_label_comment(line)
    if trailing:
        m = _ENDIF_LABEL_COMMENT_RE.match(line)
        assert m is not None
        return f"{m.group(1)} {trailing}"
    return strip_comments(line)


def strip_comments_for_instance_scan(text: str) -> str:
    """Comment strip for instance/ifdef scan; preserves RTL after `` `endif//label``."""
    return _strip_comments_stateful(text, preserve_endif_label=True)


def _define_active(name: str, defines: Mapping[str, str]) -> bool:
    if not name or name not in defines:
        return False
    val = str(defines[name]).strip().lower()
    return val not in ("", "0", "false", "1'b0", "'b0", "1'h0", "'h0")


def _apply_ifdef_directive(
    cmd: str,
    macro: str,
    stack: List[Tuple[bool, bool, bool]],
    defs: Mapping[str, str],
) -> None:
    parent = all(frame[1] for frame in stack)
    if cmd == "ifdef":
        take = parent and _define_active(macro, defs)
        stack.append((parent, take, take))
    elif cmd == "ifndef":
        take = parent and not _define_active(macro, defs)
        stack.append((parent, take, take))
    elif cmd == "elsif":
        if stack:
            p_active, _, closed = stack[-1]
            if closed:
                stack[-1] = (p_active, False, True)
            else:
                take = p_active and _define_active(macro, defs)
                stack[-1] = (p_active, take, take)
    elif cmd == "else":
        if stack:
            p_active, _, closed = stack[-1]
            if closed:
                stack[-1] = (p_active, False, True)
            else:
                stack[-1] = (p_active, p_active, True)
    elif cmd == "endif" and stack:
        stack.pop()


def _emit_ifdef_line_segments(
    line: str,
    stack: List[Tuple[bool, bool, bool]],
    defs: Mapping[str, str],
    *,
    preprocessed: bool = False,
) -> List[str]:
    """Split one source line on inline `` `ifdef `` directives; emit active segments."""
    if not preprocessed:
        line = strip_line_for_ifdef_scan(line)
    segments: List[str] = []
    pos = 0
    while True:
        m = _IFDEF_RE.search(line, pos)
        if not m:
            rest = line[pos:].strip()
            if rest and all(frame[1] for frame in stack):
                segments.append(rest)
            break
        before = line[pos : m.start()].strip()
        if before and all(frame[1] for frame in stack):
            segments.append(before)
        raw = m.group(0).lower()
        if raw.startswith("`ifdef"):
            cmd, macro = "ifdef", (m.group(1) or "").strip()
        elif raw.startswith("`ifndef"):
            cmd, macro = "ifndef", (m.group(1) or "").strip()
        elif raw.startswith("`elsif"):
            cmd, macro = "elsif", (m.group(2) or "").strip()
        elif raw.startswith("`else"):
            cmd, macro = "else", ""
        else:
            cmd, macro = "endif", ""
        _apply_ifdef_directive(cmd, macro, stack, defs)
        pos = m.end()
    return segments


def _stack_active(stack: Sequence[Tuple[bool, bool, bool]]) -> bool:
    return all(frame[1] for frame in stack)


def _expand_macros_on_line(line: str, defines: Mapping[str, str]) -> str:
    if not defines or "`" not in line:
        return line
    skip = {
        "ifdef", "ifndef", "elsif", "else", "endif",
        "define", "undef", "include",
    }

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in skip:
            return m.group(0)
        if name not in defines:
            return m.group(0)
        body = str(defines[name])
        if body.lstrip().startswith("("):
            return m.group(0)
        return body

    return _MACRO_USE_RE.sub(repl, line)


def _match_define_line(line: str) -> Optional[Tuple[str, str]]:
    dm = _DEFINE_LINE_RE.match(line)
    if dm is None:
        return None
    return dm.group(1), (dm.group(2) or "1").strip()


def _match_undef_line(line: str) -> Optional[str]:
    um = _UNDEF_LINE_RE.match(line)
    if um is None:
        return None
    return um.group(1)


def _apply_ifdef_control_line(
    line: str,
    stack: List[Tuple[bool, bool, bool]],
    defs: Mapping[str, str],
) -> bool:
    """Update *stack* when *line* is a standalone `` `ifdef `` control directive."""
    stripped = line.strip()
    m = re.match(r"^\s*`ifdef\s+([A-Za-z_]\w*)\s*$", stripped, re.IGNORECASE)
    if m:
        _apply_ifdef_directive("ifdef", m.group(1), stack, defs)
        return True
    m = re.match(r"^\s*`ifndef\s+([A-Za-z_]\w*)\s*$", stripped, re.IGNORECASE)
    if m:
        _apply_ifdef_directive("ifndef", m.group(1), stack, defs)
        return True
    m = re.match(r"^\s*`elsif\s+([A-Za-z_]\w*)\s*$", stripped, re.IGNORECASE)
    if m:
        _apply_ifdef_directive("elsif", m.group(1), stack, defs)
        return True
    m = re.match(r"^\s*`else\s*$", stripped, re.IGNORECASE)
    if m:
        _apply_ifdef_directive("else", "", stack, defs)
        return True
    m = re.match(r"^\s*`(?:endif|end)\b", stripped, re.IGNORECASE)
    if m:
        _apply_ifdef_directive("endif", "", stack, defs)
        return True
    return False


def _read_raw_source_file(
    path: Path,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    """Comment-stripped RTL source without include/define/ifdef preprocessing."""
    key = path.resolve()
    if _should_skip_preprocess_path(key, skip_path_patterns):
        return _IGNORE_PATH_STUB
    cache_key = _include_cache_key(key, skip_path_patterns)
    if cache_key is not None:
        hit = _INCLUDE_UNIT_CACHE.get(cache_key)
        if hit is not None:
            return hit
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    text = strip_comments_for_instance_scan(raw)
    if cache_key is not None:
        _INCLUDE_UNIT_CACHE[cache_key] = text
    return text


def _match_include_line(line: str) -> Optional[Tuple[str, str]]:
    m = _INCLUDE_LINE_PARSE_RE.match(line.strip())
    if m is None:
        return None
    return m.group(1), m.group(2).strip()


def _expand_macros_with_repass(
    text: str,
    defines: MutableMapping[str, str],
    *,
    apply_ifdef: bool,
    max_rounds: int = 8,
) -> str:
    """Expand macros; if expansion embeds `` `ifdef `` text, re-run conditional pass."""
    cur = text
    for _ in range(max_rounds):
        nxt = _expand_macros_on_line(cur, defines)
        if nxt == cur:
            break
        if "`" in nxt and _IFDEF_RE.search(nxt):
            nxt = _preprocess_conditional_pass(
                nxt,
                defines,
                apply_ifdef=apply_ifdef,
            )
        cur = nxt
    return cur


def _apply_inline_define_undef_segment(
    segment: str,
    stack: List[Tuple[bool, bool, bool]],
    defines: MutableMapping[str, str],
) -> None:
    if not segment or not _stack_active(stack):
        return
    for dm in _DEFINE_INLINE_RE.finditer(segment):
        defines[dm.group(1)] = (dm.group(2) or "1").strip()
    for um in _UNDEF_INLINE_RE.finditer(segment):
        defines.pop(um.group(1), None)


def _reconstruct_preserve_ifdef_line(
    line: str,
    stack: List[Tuple[bool, bool, bool]],
    defines: MutableMapping[str, str],
) -> str:
    """Keep all conditional branches; update stack; macro-expand active spans only."""
    pieces: List[str] = []
    pos = 0
    while True:
        m = _IFDEF_RE.search(line, pos)
        if not m:
            rest = line[pos:]
            if rest:
                _apply_inline_define_undef_segment(rest, stack, defines)
                if _stack_active(stack):
                    pieces.append(
                        _expand_macros_with_repass(rest, defines, apply_ifdef=False)
                    )
                else:
                    pieces.append(rest)
            break
        before = line[pos : m.start()]
        if before:
            _apply_inline_define_undef_segment(before, stack, defines)
            if _stack_active(stack):
                pieces.append(
                    _expand_macros_with_repass(before, defines, apply_ifdef=False)
                )
            else:
                pieces.append(before)
        pieces.append(line[m.start() : m.end()])
        raw = m.group(0).lower()
        if raw.startswith("`ifdef"):
            _apply_ifdef_directive("ifdef", (m.group(1) or "").strip(), stack, defines)
        elif raw.startswith("`ifndef"):
            _apply_ifdef_directive("ifndef", (m.group(1) or "").strip(), stack, defines)
        elif raw.startswith("`elsif"):
            _apply_ifdef_directive("elsif", (m.group(2) or "").strip(), stack, defines)
        elif raw.startswith("`else"):
            _apply_ifdef_directive("else", "", stack, defines)
        else:
            _apply_ifdef_directive("endif", "", stack, defines)
        pos = m.end()
    return "".join(pieces)


def _line_has_inline_ifdef(line: str) -> bool:
    return bool(_IFDEF_RE.search(line))


def _enqueue_include_lines(
    queue: deque[Tuple[str, Path]],
    *,
    inc_path: Path,
    skip_path_patterns: Sequence[str],
) -> None:
    raw = _read_raw_source_file(inc_path, skip_path_patterns)
    if not raw:
        return
    for line in reversed(list(_iter_text_lines(raw))):
        queue.appendleft((line, inc_path))


def _preprocess_conditional_pass(
    text: str,
    defines: MutableMapping[str, str],
    *,
    apply_ifdef: bool,
    source_file: Optional[Path] = None,
    include_dirs: Sequence[Path] = (),
    visiting: Optional[Set[Path]] = None,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    """
    Single ordered pass: `` `ifdef `` → `` `define ``/`` `undef `` → `` `include `` → macros.

    ``include`` expands only in active conditional branches (when *source_file* is set).
    """
    if not text:
        return text
    needs_comment_strip = "/*" in text or "//" in text
    if needs_comment_strip:
        text = strip_comments_for_instance_scan(text)
    stack: List[Tuple[bool, bool, bool]] = []
    out = StringIO()
    first = True
    src = source_file if source_file is not None else Path(".")
    line_queue: deque[Tuple[str, Path]] = deque(
        (raw_line, src) for raw_line in _iter_text_lines(text)
    )
    visiting_paths: Set[Path] = set(visiting or ())
    if source_file is not None:
        try:
            visiting_paths.add(source_file.resolve())
        except OSError:
            pass

    def _write(segment: str) -> None:
        nonlocal first
        if not segment:
            return
        if first:
            first = False
        else:
            out.write("\n")
        out.write(segment)

    while line_queue:
        raw_line, line_src = line_queue.popleft()
        if needs_comment_strip or rtl_after_ifdef_label_comment(raw_line):
            line = strip_line_for_ifdef_scan(raw_line)
        else:
            line = raw_line.strip()
        if not line:
            continue

        define_hit = _match_define_line(line)
        if define_hit is not None:
            if _stack_active(stack):
                defines[define_hit[0]] = define_hit[1]
            continue
        undef_name = _match_undef_line(line)
        if undef_name is not None:
            if _stack_active(stack):
                defines.pop(undef_name, None)
            continue

        inc = _match_include_line(line)
        if inc is not None:
            if not _stack_active(stack):
                continue
            if source_file is None:
                continue
            bracket, name = inc
            inc_path = _resolve_include(name, bracket, line_src, include_dirs)
            if inc_path is None:
                _write(f"/* hierwalk: missing include {name} */")
                continue
            try:
                inc_key = inc_path.resolve()
            except OSError:
                inc_key = inc_path
            if inc_key in visiting_paths:
                _write(f"/* hierwalk: include cycle {inc_path} */")
                continue
            if _should_skip_preprocess_path(inc_key, skip_path_patterns):
                _write(_IGNORE_PATH_STUB)
                continue
            visiting_paths.add(inc_key)
            _enqueue_include_lines(
                line_queue,
                inc_path=inc_path,
                skip_path_patterns=skip_path_patterns,
            )
            continue

        if apply_ifdef:
            segments = _emit_ifdef_line_segments(line, stack, defines, preprocessed=True)
            if not segments:
                continue
            joined = " ".join(
                _expand_macros_with_repass(seg, defines, apply_ifdef=True)
                for seg in segments
            )
            _write(joined)
            continue

        if _apply_ifdef_control_line(line, stack, defines):
            _write(line)
            continue

        if _line_has_inline_ifdef(line):
            _write(_reconstruct_preserve_ifdef_line(line, stack, defines))
            continue

        if _stack_active(stack):
            _write(_expand_macros_with_repass(line, defines, apply_ifdef=False))
        else:
            _write(line)

    return out.getvalue()


def apply_ifdef_filter(text: str, defines: Mapping[str, str]) -> str:
    defs: Dict[str, str] = dict(defines)
    return _preprocess_conditional_pass(text, defs, apply_ifdef=True)


def _resolve_include(
    name: str,
    bracket: str,
    source_file: Path,
    include_dirs: Sequence[Path],
) -> Optional[Path]:
    if bracket == "<":
        for d in include_dirs:
            p = (d / name).resolve()
            if p.is_file():
                return p
        return None
    p = (source_file.parent / name).resolve()
    if p.is_file():
        return p
    for d in include_dirs:
        p = (d / name).resolve()
        if p.is_file():
            return p
    return None


def _apply_define_ops(
    defines: MutableMapping[str, str],
    ops: Sequence[_DefineOp],
) -> None:
    for kind, name, val in ops:
        if kind == "set":
            defines[name] = val
        else:
            defines.pop(name, None)


def _collect_define_undef_ops(
    text: str,
) -> Tuple[str, Tuple[_DefineOp, ...]]:
    """Strip `` `define `` / `` `undef `` / `` `include `` lines; record define ops."""
    out = StringIO()
    ops: List[_DefineOp] = []
    first = True
    for line in _iter_text_lines(text):
        dm = _DEFINE_LINE_RE.match(line)
        if dm:
            name = dm.group(1)
            val = (dm.group(2) or "1").strip()
            ops.append(("set", name, val))
            continue
        um = _UNDEF_LINE_RE.match(line)
        if um:
            ops.append(("undef", um.group(1), ""))
            continue
        if _INCLUDE_LINE_RE.match(line):
            continue
        if first:
            first = False
        else:
            out.write("\n")
        out.write(line)
    return out.getvalue(), tuple(ops)


def _collect_define_undef(
    text: str, defines: MutableMapping[str, str]
) -> str:
    """Apply in-file `` `define `` / `` `undef `` directives; strip those lines."""
    cleaned, ops = _collect_define_undef_ops(text)
    _apply_define_ops(defines, ops)
    return cleaned


def _expand_macros(text: str, defines: Mapping[str, str]) -> str:
    """Replace `` `MACRO `` tokens (non function-like)."""
    if not defines or "`" not in text:
        return text
    skip = {
        "ifdef", "ifndef", "elsif", "else", "endif",
        "define", "undef", "include",
    }

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in skip:
            return m.group(0)
        if name not in defines:
            return m.group(0)
        body = str(defines[name])
        if body.lstrip().startswith("("):
            return m.group(0)
        return body

    out = StringIO()
    first = True
    for line in _iter_text_lines(text):
        if "`" in line:
            line = _MACRO_USE_RE.sub(repl, line)
        if first:
            first = False
        else:
            out.write("\n")
        out.write(line)
    return out.getvalue()


def _should_skip_preprocess_path(
    path: Path | str,
    skip_path_patterns: Sequence[str],
) -> bool:
    """Skip when the resolved absolute path matches ignore-path folder patterns."""
    if not skip_path_patterns:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        resolved = Path(path)
    return source_path_matches(resolved, skip_path_patterns)


def _expand_includes_once(
    text: str,
    source_file: Path,
    include_dirs: Sequence[Path],
    defines: MutableMapping[str, str],
    visiting: Set[Path],
    *,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    out = StringIO()
    last = 0
    for m in _INCLUDE_RE.finditer(text):
        out.write(text[last : m.start()])
        inc_path = _resolve_include(m.group(2).strip(), m.group(1), source_file, include_dirs)
        if inc_path is None:
            out.write(f"/* hierwalk: missing include {m.group(2)} */")
        elif _should_skip_preprocess_path(inc_path.resolve(), skip_path_patterns):
            out.write(_IGNORE_PATH_STUB)
        else:
            out.write(
                _preprocess_include_unit(
                    inc_path,
                    include_dirs,
                    defines,
                    visiting,
                    skip_path_patterns=skip_path_patterns,
                )
            )
        last = m.end()
    out.write(text[last:])
    return out.getvalue()


def _include_cache_key(
    path: Path,
    skip_path_patterns: Sequence[str] = (),
) -> Optional[_IncludeCacheKey]:
    try:
        st = path.stat()
        return (
            str(path.resolve()),
            st.st_mtime_ns,
            st.st_size,
            tuple(skip_path_patterns),
        )
    except OSError:
        return None


def _expand_include_text(
    text: str,
    path: Path,
    include_dirs: Sequence[Path],
    defines: MutableMapping[str, str],
    visiting: Set[Path],
    *,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    for _ in range(32):
        expanded = _expand_includes_once(
            text,
            path,
            include_dirs,
            defines,
            visiting,
            skip_path_patterns=skip_path_patterns,
        )
        if expanded == text:
            break
        text = expanded
    return text


def _preprocess_include_unit(
    path: Path,
    include_dirs: Sequence[Path],
    defines: MutableMapping[str, str],
    visiting: Set[Path],
    *,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    """Legacy alias: raw source read (includes run in conditional pass)."""
    _ = (include_dirs, defines, visiting)
    return _read_raw_source_file(path, skip_path_patterns)


def preprocess_file_for_index(
    path: Path,
    include_dirs: Sequence[Path],
    defines: MutableMapping[str, str],
    visiting: Optional[Set[Path]] = None,
    *,
    skip_path_patterns: Sequence[str] = (),
    apply_ifdef: Optional[bool] = None,
) -> str:
    """
    Light preprocess for index/instance scan: includes, macro expand, optional ``ifdef``.

    In-file / filelist `` `define `` names are expanded so instance scan can see
    `` `CELL u`` pairs under `` `ifndef ``. Bind stripping stays deferred to
    connect/elab. With lazy on, ``ifdef`` is also deferred unless
    ``HIERWALK_LAZY_IFDEF=1``.
    """
    if _should_skip_preprocess_path(path, skip_path_patterns):
        return _IGNORE_PATH_STUB
    from hierwalk.lazy_scope import lazy_index_ifdef

    use_ifdef = lazy_index_ifdef() if apply_ifdef is None else apply_ifdef
    base_defines = dict(defines)
    mode = "light-ifdef" if use_ifdef else "minimal"
    cache_key = _source_preprocess_cache_key(
        path, base_defines, mode, skip_path_patterns
    )
    if cache_key is not None:
        hit = _SOURCE_PREPROCESS_CACHE.get(cache_key)
        if hit is not None:
            return _restore_source_preprocess_cache_hit(defines, hit)
    visiting = visiting or set()
    raw = _read_raw_source_file(path, skip_path_patterns)
    text = _preprocess_conditional_pass(
        raw,
        defines,
        apply_ifdef=use_ifdef,
        source_file=path,
        include_dirs=include_dirs,
        visiting=visiting,
        skip_path_patterns=skip_path_patterns,
    )
    if cache_key is not None:
        _SOURCE_PREPROCESS_CACHE[cache_key] = (
            text,
            _defines_delta(base_defines, defines),
        )
    return text


def preprocess_file(
    path: Path,
    include_dirs: Sequence[Path],
    defines: MutableMapping[str, str],
    visiting: Optional[Set[Path]] = None,
    *,
    skip_path_patterns: Sequence[str] = (),
) -> str:
    """Full preprocess for one translation unit."""
    if _should_skip_preprocess_path(path, skip_path_patterns):
        return _IGNORE_PATH_STUB
    base_defines = dict(defines)
    cache_key = _source_preprocess_cache_key(
        path, base_defines, "full", skip_path_patterns
    )
    if cache_key is not None:
        hit = _SOURCE_PREPROCESS_CACHE.get(cache_key)
        if hit is not None:
            return _restore_source_preprocess_cache_hit(defines, hit)
    visiting = visiting or set()
    raw = _read_raw_source_file(path, skip_path_patterns)
    text = _preprocess_conditional_pass(
        raw,
        defines,
        apply_ifdef=True,
        source_file=path,
        include_dirs=include_dirs,
        visiting=visiting,
        skip_path_patterns=skip_path_patterns,
    )
    if re.search(r"^\s*bind\b", text, re.IGNORECASE | re.MULTILINE):
        text = _BIND_LINE_RE.sub("", text)
    if cache_key is not None:
        _SOURCE_PREPROCESS_CACHE[cache_key] = (
            text,
            _defines_delta(base_defines, defines),
        )
    return text


def _resolve_preprocess_jobs(jobs: int, num_tasks: int) -> int:
    if jobs < 0:
        return 1
    if jobs == 0:
        cpu = os.cpu_count() or 1
        return max(1, min(cpu, num_tasks))
    return max(1, min(jobs, num_tasks))


def _preprocess_file_task(
    item: Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...], Tuple[str, ...]],
) -> Tuple[str, str]:
    src, inc_dirs, define_items, skip_patterns = item
    sp = Path(src)
    inc = [Path(p) for p in inc_dirs]
    defs: Dict[str, str] = dict(define_items)
    from hierwalk.lazy_scope import lazy_processing_enabled

    preprocess_fn = (
        preprocess_file_for_index if lazy_processing_enabled() else preprocess_file
    )
    return str(sp.resolve()), preprocess_fn(
        sp,
        inc,
        defs,
        set(),
        skip_path_patterns=skip_patterns,
    )


def _includes_in_file(
    path: Path,
    source_file: Path,
    include_dirs: Sequence[Path],
) -> List[Path]:
    """Line-oriented `` `include `` discovery (closure scan only; no full-file read)."""
    found: List[Path] = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = _LINE_COMMENT_RE.sub("", raw_line)
                for m in _INCLUDE_RE.finditer(line):
                    inc_path = _resolve_include(
                        m.group(2).strip(),
                        m.group(1),
                        source_file,
                        include_dirs,
                    )
                    if inc_path is not None:
                        found.append(inc_path)
    except OSError:
        pass
    return found


def _enqueue_include(
    inc_path: Path,
    *,
    seen: Set[Path],
    queue: List[Path],
    skip_path_patterns: Sequence[str],
    max_includes: Optional[int],
) -> int:
    """Add include to closure; return 1 if skipped by ignore-path."""
    try:
        key = inc_path.resolve()
    except OSError:
        key = inc_path
    if _should_skip_preprocess_path(key, skip_path_patterns):
        return 1
    if key not in seen:
        seen.add(key)
        queue.append(key)
    return 0


def _collect_include_closure(
    sources: Sequence[str | Path],
    include_dirs: Sequence[Path],
    *,
    skip_path_patterns: Sequence[str] = (),
    max_includes: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    jobs: int = 0,
    file_via_filelist: Optional[Mapping[str, str]] = None,
    progress_every: int = 100,
) -> Tuple[List[Path], int]:
    """Discover unique `` `include `` files reachable from RTL sources (light read)."""
    src_list = [str(Path(s)) for s in sources]
    total = len(src_list)
    seen: Set[Path] = set()
    queue: List[Path] = []
    skipped = 0

    def _over_cap() -> bool:
        return max_includes is not None and len(seen) >= max_includes

    def _scan_source(src: str) -> Tuple[List[Path], int]:
        sp = Path(src)
        if _should_skip_preprocess_path(sp, skip_path_patterns):
            return [], 0
        found: List[Path] = []
        skip = 0
        for inc_path in _includes_in_file(sp, sp, include_dirs):
            try:
                key = inc_path.resolve()
            except OSError:
                key = inc_path
            if _should_skip_preprocess_path(key, skip_path_patterns):
                skip += 1
                continue
            found.append(key)
        return found, skip

    workers = _resolve_preprocess_jobs(jobs, total)
    if workers > 1 and total > 1:
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for i, (found, skip) in enumerate(
                    pool.map(_scan_source, src_list),
                    start=1,
                ):
                    skipped += skip
                    for key in found:
                        if key not in seen:
                            seen.add(key)
                            queue.append(key)
                        if _over_cap():
                            break
                    if on_progress and (i == total or i % progress_every == 0):
                        loc = format_work_location(
                            src_list[i - 1],
                            index=i,
                            total=total,
                            via_map=file_via_filelist,
                        )
                        on_progress(
                            f"preprocess: include discovery {i}/{total} — {loc}"
                        )
                    if _over_cap():
                        return list(seen), skipped
        except (OSError, PermissionError, RuntimeError):
            workers = 1

    if workers == 1:
        for i, src in enumerate(src_list, start=1):
            found, skip = _scan_source(src)
            skipped += skip
            for key in found:
                if key not in seen:
                    seen.add(key)
                    queue.append(key)
                if _over_cap():
                    return list(seen), skipped
            if on_progress and (i == total or i % progress_every == 0):
                loc = format_work_location(
                    src,
                    index=i,
                    total=total,
                    via_map=file_via_filelist,
                )
                on_progress(f"preprocess: include discovery {i}/{total} — {loc}")

    idx = 0
    while idx < len(queue):
        if _over_cap():
            break
        path = queue[idx]
        idx += 1
        for inc_path in _includes_in_file(path, path, include_dirs):
            skipped += _enqueue_include(
                inc_path,
                seen=seen,
                queue=queue,
                skip_path_patterns=skip_path_patterns,
                max_includes=max_includes,
            )
            if _over_cap():
                break
    return list(seen), skipped


from hierwalk.perf import DEFAULT_INCLUDE_WARM_MAX as _DEFAULT_INCLUDE_WARM_MAX


def _include_warm_policy() -> Tuple[bool, Optional[int]]:
    """Return ``(enabled, cap)``; warm is opt-in via ``HIERWALK_INCLUDE_WARM=1``."""
    from hierwalk.perf import include_warm_enabled

    if os.environ.get("HIERWALK_NO_INCLUDE_WARM", "").strip().lower() in (
        "1",
        "yes",
        "true",
        "on",
    ):
        return False, None
    if not include_warm_enabled():
        return False, None
    raw = os.environ.get("HIERWALK_INCLUDE_WARM_MAX", "").strip()
    if not raw:
        return True, _DEFAULT_INCLUDE_WARM_MAX
    try:
        cap = int(raw)
    except ValueError:
        return True, _DEFAULT_INCLUDE_WARM_MAX
    if cap == 0:
        return True, None
    return True, max(1, cap)


def _warm_include_unit_task(
    item: Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...], Tuple[str, ...]],
) -> str:
    src, inc_dirs, define_items, skip_patterns = item
    path = Path(src)
    inc = [Path(p) for p in inc_dirs]
    defs: Dict[str, str] = dict(define_items)
    _preprocess_include_unit(
        path,
        inc,
        defs,
        set(),
        skip_path_patterns=skip_patterns,
    )
    return str(path.resolve())


def _warm_include_cache_for_sources(
    sources: Sequence[str | Path],
    include_dirs: Sequence[Path],
    base_defines: Mapping[str, str],
    *,
    skip_path_patterns: Sequence[str] = (),
    jobs: int = 0,
    on_progress: Optional[Callable[[str], None]] = None,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> int:
    """
    Pre-expand shared includes once in the parent process.

    Workers receive this cache via pool initializer (``spawn``) or ``fork`` COW.
    """
    warm_enabled, warm_cap = _include_warm_policy()
    if not warm_enabled:
        if on_progress:
            if os.environ.get("HIERWALK_NO_INCLUDE_WARM", "").strip().lower() in (
                "1",
                "yes",
                "true",
                "on",
            ):
                on_progress("preprocess: skip include warm (HIERWALK_NO_INCLUDE_WARM)")
            else:
                on_progress(
                    "preprocess: skip include warm "
                    "(set HIERWALK_INCLUDE_WARM=1 to enable)"
                )
        return 0

    discover_cap = (warm_cap + 1) if warm_cap is not None else None
    if on_progress and sources:
        on_progress(
            f"preprocess: include discovery 0/{len(sources)} sources "
            f"(cap={warm_cap if warm_cap is not None else 'none'})"
        )
    closure, skipped = _collect_include_closure(
        sources,
        include_dirs,
        skip_path_patterns=skip_path_patterns,
        max_includes=discover_cap,
        on_progress=on_progress,
        jobs=jobs,
        file_via_filelist=file_via_filelist,
    )
    if skip_path_patterns and on_progress and skipped > 0:
        on_progress(
            f"preprocess: ignore-path skips {skipped} included file(s) "
            f"(resolved absolute path)"
        )
    if not closure:
        return 0

    if warm_cap is not None and len(closure) > warm_cap:
        if on_progress:
            on_progress(
                f"preprocess: partial include warm ({warm_cap}/{len(closure)} includes; "
                f"set HIERWALK_INCLUDE_WARM_MAX=0 for full warm)"
            )
        closure = closure[:warm_cap]

    workers = _resolve_preprocess_jobs(jobs, len(closure))
    if on_progress:
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"preprocess: warming {len(closure)} shared include(s) "
            f"({workers} workers, jobs={jobs_note})"
        )
    define_items = tuple(sorted(base_defines.items()))
    inc_dirs = tuple(str(p) for p in include_dirs)
    skip_tuple = tuple(skip_path_patterns)
    warm_tasks = [
        (str(path), inc_dirs, define_items, skip_tuple) for path in closure
    ]
    if workers == 1 or len(warm_tasks) <= 1:
        for task in warm_tasks:
            _warm_include_unit_task(task)
    else:
        try:
            from hierwalk.manifest import scan_chunksize

            chunk = scan_chunksize(len(warm_tasks), workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_warm_include_unit_task, task)
                    for task in warm_tasks
                ]
                for _ in as_completed(futures):
                    pass
        except (OSError, PermissionError, RuntimeError):
            for task in warm_tasks:
                _warm_include_unit_task(task)
    return len(closure)


def _run_preprocess_tasks_serial(
    tasks: List[Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...]]],
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    progress_every: int = 500,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    total = len(tasks)
    for i, task in enumerate(tasks, start=1):
        key, text = _preprocess_file_task(task)
        out[key] = text
        maybe_track_work(
            on_progress,
            task[0],
            index=i,
            total=total,
            via_map=file_via_filelist,
        )
        if on_progress and (i == total or i % progress_every == 0):
            loc = format_work_location(
                task[0],
                index=i,
                total=total,
                via_map=file_via_filelist,
            )
            on_progress(f"preprocess: {i}/{total} sources — {loc}")
    return out


def preprocess_sources(
    sources: Sequence[str | Path],
    include_dirs: Sequence[str | Path],
    base_defines: Mapping[str, str],
    *,
    jobs: int = 0,
    skip_path_patterns: Sequence[str] = (),
    on_progress: Optional[Callable[[str], None]] = None,
    progress_every: int = 500,
    file_via_filelist: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return map of source path → preprocessed text."""
    t0 = time.perf_counter()
    inc = [Path(p) for p in include_dirs]
    define_items = tuple(sorted(base_defines.items()))
    inc_dirs = tuple(str(p) for p in inc)
    src_list = [str(Path(s)) for s in sources]
    total = len(src_list)
    workers = _resolve_preprocess_jobs(jobs, total)
    if on_progress and total:
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"preprocess: 0/{total} sources "
            f"({workers} workers, jobs={jobs_note})"
        )

    skip_tuple = tuple(skip_path_patterns)
    _warm_include_cache_for_sources(
        src_list,
        inc,
        base_defines,
        skip_path_patterns=skip_tuple,
        jobs=jobs,
        on_progress=on_progress,
        file_via_filelist=file_via_filelist,
    )

    tasks = [(src, inc_dirs, define_items, skip_tuple) for src in src_list]
    if workers == 1 or total <= 1:
        out = _run_preprocess_tasks_serial(
            tasks,
            on_progress=on_progress,
            progress_every=progress_every,
            file_via_filelist=file_via_filelist,
        )
    else:
        out = {}
        try:
            from hierwalk.manifest import scan_chunksize

            chunk = scan_chunksize(total, workers)
            cache_snapshot = _snapshot_include_cache()
            source_cache_snapshot = _snapshot_source_preprocess_cache()
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_install_preprocess_caches,
                initargs=(cache_snapshot, source_cache_snapshot),
            ) as pool:
                for i, (key, text) in enumerate(
                    pool.map(_preprocess_file_task, tasks, chunksize=chunk),
                    start=1,
                ):
                    out[key] = text
                    maybe_track_work(
                        on_progress,
                        key,
                        index=i,
                        total=total,
                        via_map=file_via_filelist,
                    )
                    if on_progress and (i == total or i % progress_every == 0):
                        loc = format_work_location(
                            key,
                            index=i,
                            total=total,
                            via_map=file_via_filelist,
                        )
                        on_progress(f"preprocess: {i}/{total} sources — {loc}")
        except (OSError, PermissionError, RuntimeError) as exc:
            msg = (
                f"preprocess: parallel workers failed ({exc!r}); "
                "retrying with thread pool"
            )
            if on_progress:
                on_progress(msg)
            else:
                from hierwalk.progress import format_hierwalk_log

                print(format_hierwalk_log(msg), file=sys.stderr, flush=True)
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for i, (key, text) in enumerate(
                        pool.map(_preprocess_file_task, tasks),
                        start=1,
                    ):
                        out[key] = text
                        maybe_track_work(
                            on_progress,
                            key,
                            index=i,
                            total=total,
                            via_map=file_via_filelist,
                        )
                        if on_progress and (i == total or i % progress_every == 0):
                            loc = format_work_location(
                                key,
                                index=i,
                                total=total,
                                via_map=file_via_filelist,
                            )
                            on_progress(f"preprocess: {i}/{total} sources — {loc}")
            except (OSError, PermissionError, RuntimeError) as exc2:
                msg2 = (
                    f"preprocess: thread pool failed ({exc2!r}); "
                    "falling back to serial"
                )
                if on_progress:
                    on_progress(msg2)
                else:
                    from hierwalk.progress import format_hierwalk_log

                    print(format_hierwalk_log(msg2), file=sys.stderr, flush=True)
                out = _run_preprocess_tasks_serial(
                    tasks,
                    on_progress=on_progress,
                    progress_every=progress_every,
                    file_via_filelist=file_via_filelist,
                )

    elapsed = time.perf_counter() - t0
    if on_progress and total:
        rate = total / elapsed if elapsed > 0 else 0.0
        jobs_note = "auto" if jobs == 0 else str(jobs)
        on_progress(
            f"preprocess: done {total} sources in {elapsed:.1f}s "
            f"({rate:.1f} files/s, {workers} workers, jobs={jobs_note})"
        )
    return out