"""Grep-first hierarchy resolution: module index + per-node file provenance."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.inst_scan import coarse_hierarchy_path, inst_base_name

_PATH_FIELDS = ("file", "hit_file", "child_decl_file", "parent_file")

_RTL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_MODULE_DECL = re.compile(
    r"^\s*(?:module|interface|program|macromodule)\s+([A-Za-z_]\w*)\b",
    re.IGNORECASE,
)
_KEYWORDS = frozenset(
    {
        "module", "endmodule", "interface", "endinterface", "program", "endprogram",
        "input", "output", "inout", "wire", "reg", "logic", "assign", "always",
        "begin", "end", "if", "else", "generate", "endgenerate", "parameter",
    }
)


def _strip_line_comment(line: str) -> str:
    if "//" in line:
        line = line.split("//", 1)[0]
    return line.rstrip()


def _read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def abs_rtl_path(path: str | Path) -> str:
    """Return a resolved absolute RTL path string."""
    if not path:
        return ""
    return str(Path(path).resolve())


def _normalize_module_index(
    index: Mapping[str, Sequence[str | Path]],
) -> Dict[str, List[str]]:
    return {
        module: [abs_rtl_path(path) for path in files]
        for module, files in index.items()
    }


def _normalize_node_paths(node: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(node)
    for key in _PATH_FIELDS:
        if key in out and out[key]:
            out[key] = abs_rtl_path(out[key])
    return out


def _normalize_resolve_result(
    result: Dict[str, Any],
    *,
    hierarchy_input: Optional[str] = None,
) -> Dict[str, Any]:
    out = dict(result)
    if hierarchy_input is not None:
        out["hierarchy_input"] = hierarchy_input
    if out.get("module_index"):
        out["module_index"] = _normalize_module_index(out["module_index"])
    if out.get("nodes"):
        out["nodes"] = [_normalize_node_paths(node) for node in out["nodes"]]
    if out.get("candidates"):
        norm_cands: List[Dict[str, Any]] = []
        for cand in out["candidates"]:
            norm = dict(cand)
            if norm.get("parent_file"):
                norm["parent_file"] = abs_rtl_path(norm["parent_file"])
            if norm.get("nodes"):
                norm["nodes"] = [_normalize_node_paths(node) for node in norm["nodes"]]
            norm_cands.append(norm)
        out["candidates"] = norm_cands
    return out


def grep_modules_in_file(path: str | Path) -> List[str]:
    """Line-grep module/interface/program declarations in one RTL file."""
    names: List[str] = []
    seen: set[str] = set()
    try:
        with Path(path).open(encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                line = _strip_line_comment(raw)
                m = _MODULE_DECL.match(line)
                if m is None:
                    continue
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    names.append(name)
    except OSError:
        return []
    return names


class _HgrepBuildProgress:
    """Thread-safe progress for grep_hie module-index build."""

    def __init__(self, total: int) -> None:
        self._lock = threading.Lock()
        self._total = total
        self._index = 0
        self._current_file = ""
        self._started = time.monotonic()
        self._heartbeat_count = 0

    def set_file(self, index: int, path: str) -> None:
        with self._lock:
            self._index = index
            self._current_file = path

    def bump_heartbeat(self) -> int:
        with self._lock:
            self._heartbeat_count += 1
            return self._heartbeat_count

    def snapshot(self) -> Tuple[int, int, str, float, int]:
        with self._lock:
            return (
                self._index,
                self._total,
                self._current_file,
                time.monotonic() - self._started,
                self._heartbeat_count,
            )


class _HgrepBuildHeartbeat:
    """Emit periodic grep_hie build progress (default 30s)."""

    def __init__(
        self,
        progress: _HgrepBuildProgress,
        *,
        on_emit: Optional[Callable[[str], None]] = None,
        interval_sec: Optional[float] = None,
    ) -> None:
        from hierwalk.perf import hgrep_heartbeat_interval_sec

        self._progress = progress
        self._on_emit = on_emit
        self._interval = (
            interval_sec
            if interval_sec is not None
            else hgrep_heartbeat_interval_sec()
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_HgrepBuildHeartbeat":
        if self._interval is None or self._progress._total <= 0:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name="hgrep-hie-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=(self._interval or 0.0) + 2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._emit_once()

    def _emit_once(self) -> None:
        idx, total, path, elapsed, _prev = self._progress.snapshot()
        count = self._progress.bump_heartbeat()
        from hierwalk.progress import format_work_location

        if path:
            detail = format_work_location(path, index=idx, total=total)
        else:
            detail = "starting"
        msg = (
            f"hgrep-hie heartbeat count={count} "
            f"files_done={idx}/{total} elapsed_sec={elapsed:.1f} {detail}"
        )
        _emit_hgrep_build_log(msg, on_emit=self._on_emit)


def _emit_hgrep_build_log(
    message: str,
    *,
    on_emit: Optional[Callable[[str], None]] = None,
) -> None:
    if not message:
        return
    from hierwalk.hierarchy_log import emit_path_walk_log

    emit_path_walk_log(message, stream=sys.stderr)
    if on_emit is not None:
        on_emit(message)


def emit_hgrep_milestone(
    stage: str,
    detail: str,
    *,
    on_emit: Optional[Callable[[str], None]] = None,
) -> None:
    """One-line grep_hie progress milestone (filelist, rtl-db, checks %, …)."""
    stage_label = str(stage or "step").strip().replace(" ", "-")
    text = str(detail or "").strip()
    msg = f"hgrep-hie milestone {stage_label}"
    if text:
        msg = f"{msg} {text}"
    _emit_hgrep_build_log(msg, on_emit=on_emit)


def build_module_index(
    rtl_paths: Sequence[str | Path],
    *,
    progress: Optional[_HgrepBuildProgress] = None,
) -> Dict[str, List[str]]:
    """Grep all RTL paths → ``{module_name: [abs_file_path, ...]}``."""
    index: Dict[str, List[str]] = {}
    total = len(rtl_paths)
    for i, raw in enumerate(rtl_paths, start=1):
        key = abs_rtl_path(raw)
        if progress is not None:
            progress.set_file(i, key)
        for name in grep_modules_in_file(key):
            bucket = index.setdefault(name, [])
            if key not in bucket:
                bucket.append(key)
    return index


def build_file_grep_index(
    module_index: Mapping[str, Sequence[str | Path]],
) -> Dict[str, Dict[str, Any]]:
    """
    Invert grep module index → ``{abs_file_path: {modules, ...}}``.

    Each value carries the absolute path again plus module declarations found
    in that file by line-grep.
    """
    file_index: Dict[str, Dict[str, Any]] = {}
    for module, files in module_index.items():
        for raw in files:
            path = abs_rtl_path(raw)
            if not path:
                continue
            entry = file_index.setdefault(
                path,
                {
                    "file": path,
                    "modules": [],
                },
            )
            if module not in entry["modules"]:
                entry["modules"].append(module)
    for entry in file_index.values():
        entry["module_count"] = len(entry["modules"])
    return file_index


def dump_file_grep_index(
    file_index: Mapping[str, Mapping[str, Any]],
    path: str | Path,
) -> str:
    """Write file-keyed grep JSON to disk; return the absolute output path."""
    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _utc_now_iso(),
        "file_count": len(file_index),
        "files": {
            abs_rtl_path(key): dict(value) for key, value in file_index.items()
        },
    }
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return str(out)


def load_file_grep_index(path: str | Path) -> Dict[str, Dict[str, Any]]:
    """Load file-keyed grep JSON written by :func:`dump_file_grep_index`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "files" in raw:
        blob = raw["files"]
    elif isinstance(raw, dict):
        blob = raw
    else:
        raise ValueError("file grep index JSON must be an object")
    return {abs_rtl_path(key): dict(value) for key, value in blob.items()}


GREP_HIE_JSON_NAME = "grep_hie.json"
GREP_HIE_SCHEMA_VERSION = 1


def resolve_grep_hie_path(work_dir: str | Path) -> Path:
    """Return ``grep_hie.json`` path under the per-top work directory."""
    return Path(work_dir).expanduser().resolve() / GREP_HIE_JSON_NAME


def grep_hie_sources_match(
    cached: Mapping[str, Any],
    sources: Sequence[str | Path],
) -> bool:
    """True when cached RTL path set matches *sources* exactly."""
    cached_paths = {abs_rtl_path(p) for p in cached.get("rtl_paths", ()) if p}
    current = {abs_rtl_path(p) for p in sources if p}
    return bool(cached_paths) and cached_paths == current


def _grep_hie_payload(
    session: "HierarchyGrepSession",
    *,
    top: str = "",
    file_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    fi = file_index
    if fi is None:
        fi = session._file_grep_index or build_file_grep_index(session.module_index)
    return {
        "schema_version": GREP_HIE_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "top": top,
        "rtl_paths": list(session.rtl_paths),
        "module_index": session.module_index,
        "files": {abs_rtl_path(key): dict(value) for key, value in fi.items()},
    }


def dump_grep_hie(
    session: "HierarchyGrepSession",
    path: str | Path,
    *,
    top: str = "",
    file_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> str:
    """Persist hierarchy grep session data to ``grep_hie.json``."""
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            _grep_hie_payload(session, top=top, file_index=file_index),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(out)


def load_grep_hie(path: str | Path) -> Dict[str, Any]:
    """Load ``grep_hie.json`` written by :func:`dump_grep_hie`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("grep_hie.json must be an object")
    if "module_index" not in raw:
        raise ValueError("grep_hie.json missing module_index")
    files = raw.get("files")
    if files is None:
        raise ValueError("grep_hie.json missing files")
    if not isinstance(files, dict):
        raise ValueError("grep_hie.json files must be an object")
    out = dict(raw)
    out["rtl_paths"] = [abs_rtl_path(p) for p in raw.get("rtl_paths", ()) if p]
    out["module_index"] = _normalize_module_index(raw["module_index"])
    out["files"] = {abs_rtl_path(key): dict(value) for key, value in files.items()}
    return out


def remove_grep_hie(path: str | Path) -> bool:
    """Delete ``grep_hie.json`` when present; return whether a file was removed."""
    p = Path(path).expanduser()
    if p.is_file():
        p.unlink()
        return True
    return False


def collect_rtl_paths(
    roots: Sequence[str | Path],
    *,
    recursive: bool = True,
) -> List[str]:
    """Expand directories / explicit RTL files into a flat path list."""
    out: List[str] = []
    seen: set[str] = set()
    for root in roots:
        p = Path(root)
        if p.is_file():
            if p.suffix.lower() in _RTL_SUFFIXES:
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(key)
            continue
        if not p.is_dir():
            continue
        globber = p.rglob if recursive else p.glob
        for child in sorted(globber("*")):
            if child.is_file() and child.suffix.lower() in _RTL_SUFFIXES:
                key = str(child.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


@dataclass
class HierarchyGrepSession:
    """
    Grep index + hierarchy resolve session.

    Builds the module→files grep index synchronously, then starts a background
    thread to materialize the file→info JSON used by downstream processing.
    """

    rtl_paths: List[str]
    module_index: Dict[str, List[str]]
    _file_grep_index: Optional[Dict[str, Dict[str, Any]]] = field(
        default=None,
        init=False,
        repr=False,
    )
    _file_grep_index_error: Optional[str] = field(default=None, init=False, repr=False)
    _file_grep_index_ready: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _file_grep_index_thread: Optional[threading.Thread] = field(
        default=None,
        init=False,
        repr=False,
    )
    file_grep_index_path: Optional[str] = field(default=None, init=False, repr=False)

    @classmethod
    def from_grep_hie_cache(
        cls,
        data: Mapping[str, Any],
        *,
        cache_path: Optional[str | Path] = None,
    ) -> HierarchyGrepSession:
        """Rehydrate a session from ``grep_hie.json`` without re-grepping RTL."""
        rtl_paths = [abs_rtl_path(p) for p in data.get("rtl_paths", ()) if p]
        module_index = _normalize_module_index(data.get("module_index", {}))
        session = cls(rtl_paths=rtl_paths, module_index=module_index)
        files = data.get("files") or {}
        session._file_grep_index = {
            abs_rtl_path(key): dict(value) for key, value in files.items()
        }
        session._file_grep_index_ready.set()
        if cache_path is not None:
            session.file_grep_index_path = abs_rtl_path(cache_path)
        return session

    @classmethod
    def from_rtl_paths(
        cls,
        rtl_paths: Sequence[str | Path],
        *,
        module_index: Optional[Mapping[str, Sequence[str | Path]]] = None,
        build_file_index_background: bool = True,
        file_grep_index_path: Optional[str | Path] = None,
        on_emit: Optional[Callable[[str], None]] = None,
    ) -> HierarchyGrepSession:
        abs_paths = [abs_rtl_path(p) for p in rtl_paths if p]
        if module_index is None:
            emit_hgrep_milestone(
                "rtl-db-build-start",
                f"rtl_files={len(abs_paths)}",
                on_emit=on_emit,
            )
            t_build = time.perf_counter()
            progress = _HgrepBuildProgress(len(abs_paths))
            with _HgrepBuildHeartbeat(progress, on_emit=on_emit):
                mod_index = build_module_index(abs_paths, progress=progress)
            emit_hgrep_milestone(
                "rtl-db-built",
                (
                    f"modules={len(mod_index)} rtl_files={len(abs_paths)} "
                    f"elapsed_sec={time.perf_counter() - t_build:.1f}"
                ),
                on_emit=on_emit,
            )
        else:
            mod_index = _normalize_module_index(module_index)
        session = cls(rtl_paths=abs_paths, module_index=mod_index)
        if file_grep_index_path:
            session.file_grep_index_path = abs_rtl_path(file_grep_index_path)
        if build_file_index_background:
            session._start_file_grep_index_background()
        else:
            session._file_grep_index = build_file_grep_index(session.module_index)
            if session.file_grep_index_path:
                dump_file_grep_index(
                    session._file_grep_index,
                    session.file_grep_index_path,
                )
            session._file_grep_index_ready.set()
        return session

    def _start_file_grep_index_background(self) -> None:
        def _run() -> None:
            try:
                self._file_grep_index = build_file_grep_index(self.module_index)
                if self.file_grep_index_path:
                    dump_file_grep_index(
                        self._file_grep_index,
                        self.file_grep_index_path,
                    )
            except Exception as exc:  # noqa: BLE001 — background best-effort
                self._file_grep_index_error = str(exc)
            finally:
                self._file_grep_index_ready.set()

        self._file_grep_index_thread = threading.Thread(
            target=_run,
            name="hierarchy-grep-file-index",
            daemon=True,
        )
        self._file_grep_index_thread.start()

    def file_grep_index(
        self,
        *,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return file-keyed grep JSON; optionally wait for background build."""
        if wait:
            self._file_grep_index_ready.wait(timeout=timeout)
        if self._file_grep_index_error:
            raise RuntimeError(self._file_grep_index_error)
        return dict(self._file_grep_index or {})

    def file_grep_index_ready(self) -> bool:
        return self._file_grep_index_ready.is_set()

    def write_file_grep_index(
        self,
        path: str | Path,
        *,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> str:
        """Persist file-keyed grep JSON and remember the output path."""
        data = self.file_grep_index(wait=wait, timeout=timeout)
        self.file_grep_index_path = dump_file_grep_index(data, path)
        return self.file_grep_index_path

    def resolve(self, hierarchy: str, *, top: str) -> Dict[str, Any]:
        return resolve_hierarchy_grep(
            hierarchy,
            top=top,
            rtl_paths=self.rtl_paths,
            module_index=self.module_index,
        )

    def resolve_with_file_index(
        self,
        hierarchy: str,
        *,
        top: str,
        wait_file_index: bool = True,
        timeout: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """Resolve hierarchy and return ``(resolve_result, file_grep_index)``."""
        result = self.resolve(hierarchy, top=top)
        file_index = self.file_grep_index(wait=wait_file_index, timeout=timeout)
        return result, file_index


def _module_body(text: str, module_name: str) -> str:
    start = re.search(
        rf"\b(?:module|interface|program)\s+{re.escape(module_name)}\b",
        text,
        re.IGNORECASE,
    )
    if start is None:
        return ""
    chunk = text[start.start() :]
    end = re.search(
        r"\b(?:endmodule|endinterface|endprogram)\b",
        chunk,
        re.IGNORECASE,
    )
    return chunk[: end.start()] if end else chunk


def _module_header(body: str, module_name: str) -> str:
    m = re.match(
        rf"\b(?:module|interface|program)\s+{re.escape(module_name)}\b(.*)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if m is None:
        return ""
    rest = m.group(1)
    semi = rest.find(";")
    return rest[:semi] if semi >= 0 else rest


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _split_hier_segment(segment: str) -> Tuple[str, Optional[str]]:
    m = re.fullmatch(r"([A-Za-z_]\w*)(?:\[(\d+)\])?", segment)
    if m is None:
        return segment, None
    return m.group(1), m.group(2)


def _cell_before_inst(compact: str, inst_at: int) -> Optional[str]:
    before = compact[:inst_at].rstrip().rstrip(",")
    while before.endswith(")"):
        hash_idx = before.rfind("#")
        open_idx = before.rfind("(")
        if hash_idx < 0 or open_idx < hash_idx:
            break
        before = before[:hash_idx].rstrip()
    m = re.search(r"([A-Za-z_]\w*)\s*$", before)
    if m is None:
        return None
    cell = m.group(1)
    if cell.lower() in _KEYWORDS:
        return None
    return cell


def _inst_child_module(body: str, inst_leaf: str) -> Optional[str]:
    """Return cell type for ``inst_leaf`` instance, if declared in *body*."""
    if not body or not inst_leaf:
        return None
    compact = _collapse_ws(body)
    base, _idx = _split_hier_segment(inst_leaf)
    needles = [inst_leaf]
    if base != inst_leaf:
        needles.append(base)
    for name in needles:
        anchor = re.compile(
            rf"\b{re.escape(name)}\s*(?:\[[^\]]*\]\s*)*\(",
            re.IGNORECASE,
        )
        for m in anchor.finditer(compact):
            cell = _cell_before_inst(compact, m.start())
            if cell:
                return cell
    return None


def _generate_block_body(
    body: str,
    block_name: str,
    *,
    index: Optional[str] = None,
) -> Optional[str]:
    """Return inner text of ``begin : block_name`` (generate label)."""
    pat = re.compile(
        rf"\bbegin\s*:\s*{re.escape(block_name)}\b",
        re.IGNORECASE,
    )
    m = pat.search(body)
    if m is None:
        return None
    start = m.end()
    depth = 1
    i = start
    n = len(body)
    while i < n and depth:
        if re.match(r"\bbegin\b", body[i:], re.I):
            depth += 1
            i += 5
            continue
        if re.match(r"\bend\b", body[i:], re.I):
            depth -= 1
            i += 3
            continue
        i += 1
    if depth != 0:
        return None
    inner = body[start : i - 3]
    if index is None:
        return inner
    return inner


def _port_names(header: str) -> set[str]:
    names: set[str] = set()
    if "(" not in header:
        return names
    port_blob = header[header.find("(") + 1 :]
    if ")" in port_blob:
        port_blob = port_blob[: port_blob.rfind(")")]
    for part in port_blob.split(","):
        part = re.sub(r"\[[^\]]*\]", " ", part)
        tokens = re.findall(r"[A-Za-z_]\w*", part)
        for tok in reversed(tokens):
            if tok.lower() not in _KEYWORDS:
                names.add(tok)
                break
    return names


def _identifiers_after_decl_keyword(seg: str, kw: str) -> List[str]:
    """Names declared after ``wire``/``output``/… on one statement (comma-separated)."""
    if not re.search(rf"\b{kw}\b", seg, re.I):
        return []
    m = re.search(rf"\b{kw}\b\s*(.*)$", seg, re.I | re.DOTALL)
    if not m:
        return []
    rest = m.group(1).strip().rstrip(";")
    rest = re.sub(r"\[[^\]]*\]", " ", rest)
    out: List[str] = []
    for part in rest.split(","):
        part = part.strip()
        if not part:
            continue
        hit = re.search(r"([A-Za-z_]\w*)", part)
        if hit and hit.group(1).lower() not in _KEYWORDS:
            out.append(hit.group(1))
    return out


def _declared_signals(body: str) -> set[str]:
    names: set[str] = set()
    decl_kws = (
        "wire",
        "reg",
        "logic",
        "wand",
        "wor",
        "input",
        "output",
        "inout",
    )
    for raw in body.splitlines():
        line = _strip_line_comment(raw).strip()
        if not line or line.startswith("`"):
            continue
        for segment in line.split(";"):
            seg = segment.strip()
            if not seg:
                continue
            low = seg.lower()
            for kw in decl_kws:
                if re.search(rf"\b{kw}\b", low):
                    names.update(_identifiers_after_decl_keyword(seg, kw))
                    break
            if re.search(r"\bassign\b", low):
                m = re.search(r"\bassign\s+([A-Za-z_]\w*)", seg, re.I)
                if m:
                    names.add(m.group(1))
    return names


def _body_cached(
    cache: Dict[Tuple[str, str], str],
    path: str,
    module_name: str,
) -> str:
    key = (path, module_name)
    hit = cache.get(key)
    if hit is not None:
        return hit
    body = _module_body(_read_text(path), module_name)
    cache[key] = body
    return body


def _scoped_body(
    cache: Dict[Tuple[str, str], str],
    path: str,
    module_name: str,
    scope_stack: Sequence[Tuple[str, Optional[str]]],
) -> str:
    scoped = _body_cached(cache, path, module_name)
    for label, idx in scope_stack:
        narrowed = _generate_block_body(scoped, label, index=idx)
        if narrowed is None:
            return ""
        scoped = narrowed
    return scoped


def _module_body_after_header(body: str, module_name: str) -> str:
    m = re.match(
        rf"\b(?:module|interface|program)\s+{re.escape(module_name)}\b[^;]*;",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if m is None:
        return body.strip()
    return body[m.end() :].strip()


def _module_body_has_content(body: str, module_name: str) -> bool:
    """True when *body* has ports, instances, or internal declarations."""
    if not body.strip():
        return False
    header = _module_header(body, module_name)
    if _port_names(header):
        return True
    inner = _module_body_after_header(body, module_name)
    if not inner:
        return False
    if _declared_signals(inner):
        return True
    compact = _collapse_ws(inner)
    return bool(
        re.search(r"\b[A-Za-z_]\w*\s+[A-Za-z_]\w*\s*(?:\(|;)", compact)
    )


def _leaf_kind_in_body(
    body: str,
    module_name: str,
    leaf: str,
) -> Tuple[Optional[str], str]:
    """Classify last path segment inside one scoped body."""
    if _inst_child_module(body, leaf):
        return "inst", f"instance {inst_base_name(leaf)}"
    leaf_base = inst_base_name(leaf)
    header = _module_header(body, module_name)
    if leaf_base in _port_names(header):
        note = f"port {leaf_base}" if leaf_base == leaf else f"port {leaf_base} (from {leaf})"
        return "port", note
    if leaf_base in _declared_signals(body):
        note = f"signal {leaf_base}" if leaf_base == leaf else f"signal {leaf_base} (from {leaf})"
        return "signal", note
    return None, f"{leaf} not found as inst/port/signal"


@dataclass
class _Branch:
    mod: str
    parent_file: str
    gen_scope: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    nodes: List[Dict[str, Any]] = field(default_factory=list)


def _branch_key(br: _Branch) -> Tuple[Any, ...]:
    return (br.mod, br.parent_file, tuple(br.gen_scope))


def _child_decl_candidates(
    child_mod: str,
    index: Mapping[str, Sequence[str]],
    cache: Dict[Tuple[str, str], str],
    *,
    prune_empty: bool,
) -> List[str]:
    out: List[str] = []
    for path in index.get(child_mod, ()):
        if prune_empty:
            body = _body_cached(cache, path, child_mod)
            if not _module_body_has_content(body, child_mod):
                continue
        out.append(path)
    return out


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _timing_fields(
    *,
    started_at: str,
    resolve_start: float,
    index_build_ms: float = 0.0,
) -> Dict[str, Any]:
    return {
        "started_at": started_at,
        "resolved_at": _utc_now_iso(),
        "index_build_ms": index_build_ms,
        "total_elapsed_ms": _elapsed_ms(resolve_start),
    }


def _stamp_branch_hop_ms(branches: Sequence[_Branch], hop_ms: float) -> None:
    for br in branches:
        if br.nodes:
            br.nodes[-1]["elapsed_ms"] = hop_ms


def _finalize_branches(
    branches: Sequence[_Branch],
) -> Tuple[bool, bool, str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(ok, ambiguous, error, nodes, candidates)``."""
    if not branches:
        return False, False, "no matching declaration branch", [], []
    uniq: Dict[Tuple[Any, ...], _Branch] = {}
    for br in branches:
        uniq[_branch_key(br)] = br
    survivors = list(uniq.values())
    if len(survivors) == 1:
        return True, False, "", list(survivors[0].nodes), []
    cand_payload = [
        {
            "parent_file": br.parent_file,
            "module": br.mod,
            "nodes": list(br.nodes),
        }
        for br in survivors
    ]
    return True, True, "", list(survivors[0].nodes), cand_payload


def resolve_hierarchy_grep(
    hierarchy: str,
    *,
    top: str,
    rtl_paths: Sequence[str | Path],
    module_index: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """
    Resolve *hierarchy* using grep-built module index and per-node file paths.

    Duplicate module declarations are explored as branches; a branch is pruned
    when the current hop (inst / port / signal) does not match in that file.
    """
    started_at = _utc_now_iso()
    resolve_start = time.perf_counter()

    hierarchy_input = hierarchy.strip()
    text = coarse_hierarchy_path(hierarchy_input)
    top_name = top.strip()
    if not text or not top_name:
        return _normalize_resolve_result(
            {
                "ok": False,
                "top": top_name,
                "hierarchy": text,
                "error": "empty hierarchy or top",
                "ambiguous": False,
                "module_index": {},
                "nodes": [],
                "candidates": [],
                **_timing_fields(started_at=started_at, resolve_start=resolve_start),
            },
            hierarchy_input=hierarchy_input,
        )

    paths = [abs_rtl_path(p) for p in rtl_paths]
    index_build_start = time.perf_counter()
    if module_index is None:
        index: Dict[str, List[str]] = build_module_index(paths)
        index_build_ms = _elapsed_ms(index_build_start)
    else:
        index = _normalize_module_index(module_index)
        index_build_ms = 0.0

    top_name = inst_base_name(top_name)
    parts = text.split(".")
    if parts[0] != top_name:
        if text.startswith(top_name + "."):
            parts = text.split(".")
        else:
            parts = [top_name, *text.split(".")] if text != top_name else [top_name]
    parts = [inst_base_name(part) for part in parts]
    text = ".".join(parts)

    body_cache: Dict[Tuple[str, str], str] = {}

    if top_name not in index:
        return _normalize_resolve_result(
            {
                "ok": False,
                "top": top_name,
                "hierarchy": text,
                "error": f"top module {top_name!r} not in grep index",
                "ambiguous": False,
                "module_index": index,
                "nodes": [],
                "candidates": [],
                **_timing_fields(
                    started_at=started_at,
                    resolve_start=resolve_start,
                    index_build_ms=index_build_ms,
                ),
            },
            hierarchy_input=hierarchy_input,
        )

    top_candidates = index[top_name]
    root_start = time.perf_counter()
    root_node = {
        "segment": top_name,
        "role": "root",
        "module": top_name,
        "file": top_candidates[0],
        "hit_file": top_candidates[0],
        "found": True,
        "elapsed_ms": _elapsed_ms(root_start),
    }
    branches = [
        _Branch(
            mod=top_name,
            parent_file=top_candidates[0],
            nodes=[root_node],
        )
    ]

    err = ""
    failed = False

    for i, seg in enumerate(parts[1:], start=1):
        hop_start = time.perf_counter()
        is_leaf = i == len(parts) - 1
        next_branches: List[_Branch] = []

        if not is_leaf:
            for br in branches:
                body = _scoped_body(body_cache, br.parent_file, br.mod, br.gen_scope)
                child_mod = _inst_child_module(body, seg)
                if child_mod:
                    for child_file in index.get(child_mod, ()):
                        node = {
                            "segment": seg,
                            "role": "inst",
                            "module": br.mod,
                            "file": br.parent_file,
                            "hit_file": br.parent_file,
                            "child_decl_file": child_file,
                            "child_module": child_mod,
                            "found": True,
                        }
                        next_branches.append(
                            _Branch(
                                mod=child_mod,
                                parent_file=child_file,
                                gen_scope=[],
                                nodes=[*br.nodes, node],
                            )
                        )
                    continue

                label, idx = _split_hier_segment(seg)
                if _generate_block_body(body, label, index=idx) is not None:
                    node = {
                        "segment": seg,
                        "role": "genblk",
                        "module": br.mod,
                        "file": br.parent_file,
                        "hit_file": br.parent_file,
                        "found": True,
                        "detail": f"generate block {seg}",
                    }
                    next_branches.append(
                        _Branch(
                            mod=br.mod,
                            parent_file=br.parent_file,
                            gen_scope=[*br.gen_scope, (label, idx)],
                            nodes=[*br.nodes, node],
                        )
                    )

            if not next_branches:
                failed = True
                err = f"instance {branches[0].mod}.{seg} not found"
                fail_mod = branches[0].mod
                fail_file = branches[0].parent_file
                hop_ms = _elapsed_ms(hop_start)
                fail_nodes = [
                    *branches[0].nodes,
                    {
                        "segment": seg,
                        "role": "inst",
                        "module": fail_mod,
                        "file": fail_file,
                        "hit_file": fail_file,
                        "found": False,
                        "detail": err,
                        "elapsed_ms": hop_ms,
                    },
                ]
                return _normalize_resolve_result(
                    {
                        "ok": False,
                        "top": top_name,
                        "hierarchy": ".".join(parts),
                        "error": err,
                        "ambiguous": False,
                        "module_index": index,
                        "nodes": fail_nodes,
                        "candidates": [],
                        **_timing_fields(
                            started_at=started_at,
                            resolve_start=resolve_start,
                            index_build_ms=index_build_ms,
                        ),
                    },
                    hierarchy_input=hierarchy_input,
                )
            hop_ms = _elapsed_ms(hop_start)
            _stamp_branch_hop_ms(next_branches, hop_ms)
            branches = next_branches
            continue

        for br in branches:
            body = _scoped_body(body_cache, br.parent_file, br.mod, br.gen_scope)
            kind, detail = _leaf_kind_in_body(body, br.mod, seg)
            if kind == "inst":
                child_mod = _inst_child_module(body, seg) or ""
                child_files = _child_decl_candidates(
                    child_mod,
                    index,
                    body_cache,
                    prune_empty=True,
                )
                if not child_files:
                    child_files = list(index.get(child_mod, ()))
                if not child_files:
                    child_files = [br.parent_file]
                for child_file in child_files:
                    node = {
                        "segment": seg,
                        "role": "leaf",
                        "kind": "inst",
                        "module": br.mod,
                        "file": br.parent_file,
                        "hit_file": br.parent_file,
                        "child_decl_file": child_file,
                        "child_module": child_mod,
                        "found": True,
                        "detail": detail,
                    }
                    next_branches.append(
                        _Branch(
                            mod=child_mod or br.mod,
                            parent_file=child_file,
                            gen_scope=[],
                            nodes=[*br.nodes, node],
                        )
                    )
            elif kind is not None:
                node = {
                    "segment": seg,
                    "role": "leaf",
                    "kind": kind,
                    "module": br.mod,
                    "file": br.parent_file,
                    "hit_file": br.parent_file,
                    "found": True,
                    "detail": detail,
                }
                next_branches.append(
                    _Branch(
                        mod=br.mod,
                        parent_file=br.parent_file,
                        gen_scope=list(br.gen_scope),
                        nodes=[*br.nodes, node],
                    )
                )
            else:
                failed = True
                err = detail

        if failed and not next_branches:
            fail_br = branches[0]
            hop_ms = _elapsed_ms(hop_start)
            fail_nodes = [
                *fail_br.nodes,
                {
                    "segment": seg,
                    "role": "leaf",
                    "kind": "missing",
                    "module": fail_br.mod,
                    "file": fail_br.parent_file,
                    "hit_file": fail_br.parent_file,
                    "found": False,
                    "detail": err,
                    "elapsed_ms": hop_ms,
                },
            ]
            return _normalize_resolve_result(
                {
                    "ok": False,
                    "top": top_name,
                    "hierarchy": ".".join(parts),
                    "error": err,
                    "ambiguous": False,
                    "module_index": index,
                    "nodes": fail_nodes,
                    "candidates": [],
                    **_timing_fields(
                        started_at=started_at,
                        resolve_start=resolve_start,
                        index_build_ms=index_build_ms,
                    ),
                },
                hierarchy_input=hierarchy_input,
            )
        hop_ms = _elapsed_ms(hop_start)
        _stamp_branch_hop_ms(next_branches, hop_ms)
        branches = next_branches

    ok, ambiguous, _, nodes, candidates = _finalize_branches(branches)
    return _normalize_resolve_result(
        {
            "ok": ok,
            "top": top_name,
            "hierarchy": ".".join(parts),
            "error": "" if ok else "no matching declaration branch",
            "ambiguous": ambiguous,
            "module_index": index,
            "nodes": nodes,
            "candidates": candidates,
            **_timing_fields(
                started_at=started_at,
                resolve_start=resolve_start,
                index_build_ms=index_build_ms,
            ),
        },
        hierarchy_input=hierarchy_input,
    )


def format_hierarchy_grep_report(result: Mapping[str, Any]) -> str:
    """Human-readable report with embedded JSON for downstream tools."""
    lines = [
        "Hierarchy grep report",
        f"  started_at: {result.get('started_at', '')}",
        f"  resolved_at: {result.get('resolved_at', '')}",
        f"  total_elapsed_ms: {result.get('total_elapsed_ms', 0.0)}",
    ]
    if result.get("index_build_ms"):
        lines.append(f"  index_build_ms: {result['index_build_ms']}")
    lines.extend(
        [
            f"  hierarchy: {result.get('hierarchy', '')}",
            f"  ok: {result.get('ok', False)}",
        ]
    )
    if result.get("hierarchy_input") and result.get("hierarchy_input") != result.get(
        "hierarchy"
    ):
        lines.append(f"  hierarchy_input: {result['hierarchy_input']}")
    if result.get("ambiguous"):
        lines.append(f"  ambiguous: {len(result.get('candidates', ()))} branches")
    if result.get("error"):
        lines.append(f"  error: {result['error']}")
    lines.append("  nodes:")
    for node in result.get("nodes", ()):
        flag = "OK" if node.get("found") else "MISS"
        role = node.get("role", "?")
        kind = node.get("kind", "")
        kind_note = f" ({kind})" if kind else ""
        hop_ms = node.get("elapsed_ms")
        hop_note = f" ({hop_ms} ms)" if hop_ms is not None else ""
        lines.append(
            f"    [{flag}] {role}{kind_note} {node.get('segment', '')} "
            f"@ {node.get('module', '')} -> {node.get('file', '')}{hop_note}"
        )
        if node.get("hit_file") and node.get("hit_file") != node.get("file"):
            lines.append(f"         hit_file={node['hit_file']}")
        if node.get("child_decl_file"):
            lines.append(f"         child_decl_file={node['child_decl_file']}")
        if node.get("child_module"):
            lines.append(f"         child={node['child_module']}")
        if node.get("detail"):
            lines.append(f"         {node['detail']}")
    if result.get("candidates"):
        lines.append(f"  candidates: {len(result['candidates'])}")
    lines.append("json:")
    lines.append(json.dumps(result, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def hierarchy_grep_report(
    hierarchy: str,
    *,
    top: str,
    rtl_paths: Sequence[str | Path],
    module_index: Optional[Mapping[str, Sequence[str]]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Run resolve + return ``(report_text, result_json)``."""
    result = resolve_hierarchy_grep(
        hierarchy,
        top=top,
        rtl_paths=rtl_paths,
        module_index=module_index,
    )
    return format_hierarchy_grep_report(result), dict(result)