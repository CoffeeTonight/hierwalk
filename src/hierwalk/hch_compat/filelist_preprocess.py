"""
Expand Verilog filelists (-f / -F) and emit pyslang-safe lines.

VCS semantics:
  -f nested.f  — locate nested.f relative to the containing .f directory;
                 paths inside nested.f are relative to nested.f's directory.
  -F nested.f  — locate nested.f relative to index_cwd (EDA run directory);
                 paths inside nested.f are relative to index_cwd.

pyslang does not implement -F; we flatten to absolute +incdir+, +define+, and
source paths (no nested -f/-F).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Union

OnFilelistProgress = Callable[[str], None]


@dataclass
class FilelistResult:
    top_path: Path
    base_dir: Path
    raw_top_filelist: str = ""
    source_files: List[Path] = field(default_factory=list)
    incdirs: List[Path] = field(default_factory=list)
    defines: Dict[str, str] = field(default_factory=dict)
    library_files: List[Path] = field(default_factory=list)
    library_dirs: List[Path] = field(default_factory=list)
    libexts: List[str] = field(default_factory=lambda: [".v", ".sv", ".vh", ".svh"])
    slang_options: List[str] = field(default_factory=list)
    unsupported_options: List[str] = field(default_factory=list)
    top_modules: List[str] = field(default_factory=list)
    work_library: str = ""
    errors: List[str] = field(default_factory=list)
    index_cwd_used: Optional[Path] = None
    source_via_filelist: Dict[Path, Path] = field(default_factory=dict)
    source_filelist_chain: Dict[Path, str] = field(default_factory=dict)
    filelist_info: Dict[Path, Dict[str, str]] = field(default_factory=dict)
    filelist_children: Dict[Path, List[Path]] = field(default_factory=dict)
    filelist_edges: List[tuple[Path, Path, str]] = field(default_factory=list)


def _strip_comments(line: str) -> str:
    line = re.sub(r"/\*.*?\*/", "", line, flags=re.DOTALL)
    if "//" in line:
        line = line.split("//", 1)[0]
    return line.strip().replace("\r", "")


_PATH_LIKE_GLUE = re.compile(r"(?:[./$\\]|^\.|\.)")


def _looks_like_filelist_path(arg: str) -> bool:
    """Glued ``-f``/``-F`` only when the token looks like a path, not ``-full64``."""
    if not arg:
        return False
    return bool(_PATH_LIKE_GLUE.search(arg))


def _dash_switch_arg(
    line: str,
    flag: str,
    *,
    allow_glued: bool = False,
) -> Optional[str]:
    """
    VCS-style ``-f``, ``-F``, ``-v``, ``-y`` lines.

    Accepts space or tab before the argument. Glued paths (``-flist.f``) are optional
    and only enabled for ``-f``/``-F`` when the suffix looks path-like.
    """
    stripped = line.strip()
    prefix = f"-{flag}"
    if not stripped.startswith(prefix):
        return None
    rest = stripped[len(prefix) :]
    if not rest:
        return None
    if rest[0] in " \t":
        rest = rest.lstrip(" \t")
        return rest if rest else None
    if not allow_glued:
        return None
    return rest if _looks_like_filelist_path(rest) else None


def _word_switch_arg(line: str, word: str) -> Optional[str]:
    """``-top name``, ``-topmodule name`` — whitespace required after the keyword."""
    stripped = line.strip()
    if not stripped.startswith(word):
        return None
    rest = stripped[len(word) :]
    if not rest or rest[0] not in " \t":
        return None
    arg = rest.lstrip(" \t")
    return arg if arg else None


def _nested_filelist_directive(line: str) -> Optional[tuple[str, str]]:
    """Parse nested ``-f`` / ``-F`` filelist includes (space, tab, or glued path)."""
    for flag in ("F", "f"):
        arg = _dash_switch_arg(line, flag, allow_glued=True)
        if arg is not None:
            return f"-{flag}", arg
    return None


def expand_filelist(
    top_filelist: Union[str, Path],
    env: Optional[Dict[str, str]] = None,
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    on_progress: Optional[OnFilelistProgress] = None,
    ignore_filelist_patterns: Optional[Sequence[str]] = None,
    defer_source_exists: bool = False,
) -> FilelistResult:
    """
    Expand a top ``.f`` into :class:`FilelistResult`.

    ``index_cwd`` is the directory tools use for ``-F`` (see :func:`filelist_cwd.resolve_index_cwd`).
    """
    from hierwalk.hch_compat.filelist_cwd import resolve_index_cwd
    from hierwalk.hch_compat.platform_paths import (
        expand_path_vars,
        merge_environ,
        normalize_filelist_token,
        resolve_path as _resolve_abs,
        unexpanded_path_vars,
    )

    raw_top = str(top_filelist)
    env_map = merge_environ(env)
    top = _resolve_abs(expand_path_vars(raw_top, env_map))
    cwd = resolve_index_cwd(top, index_cwd, env_map)
    result = FilelistResult(top_path=top, base_dir=top.parent, raw_top_filelist=raw_top)
    seen_fl: Set[Path] = set()
    seen_src: Set[Path] = set()

    def resolve_path(raw: str, base: Path) -> Path:
        raw = expand_path_vars(raw, env_map)
        p = Path(raw)
        if not p.is_absolute():
            p = base / p
        return _resolve_abs(p)

    from hierwalk.ignore_path import filelist_path_matches

    ignore_fl = list(ignore_filelist_patterns or ())

    def _skip_nested_filelist(fpath: Path, chain_text: str) -> bool:
        if not ignore_fl:
            return False
        return filelist_path_matches(fpath, chain=chain_text, patterns=ignore_fl)

    def add_source(sp: Path, *, via_filelist: Path, chain: List[Path]) -> None:
        if sp in seen_src:
            return
        seen_src.add(sp)
        result.source_via_filelist[sp] = via_filelist
        result.source_filelist_chain[sp] = " -> ".join(str(p) for p in chain)
        if defer_source_exists:
            result.source_files.append(sp)
        elif sp.exists():
            result.source_files.append(sp)
        else:
            msg = f"Source not found: {sp}"
            result.errors.append(msg)
            if on_progress:
                on_progress(f"filelist: missing source {sp}")

    def add_incdir(ip: Path) -> None:
        if ip not in result.incdirs:
            result.incdirs.append(ip)

    def link_nested(parent: Path, child: Path, kind: str) -> None:
        parent_k = parent.resolve()
        child_k = child.resolve()
        kids = result.filelist_children.setdefault(parent_k, [])
        if child_k not in kids:
            kids.append(child_k)
        edge = (parent_k, child_k, kind)
        if edge not in result.filelist_edges:
            result.filelist_edges.append(edge)

    def register_filelist(
        fpath: Path,
        *,
        chain: List[Path],
        parent: Optional[Path],
        include_kind: str,
    ) -> None:
        key = fpath.resolve()
        if key in result.filelist_info:
            return
        chain_out = chain if chain else [key]
        result.filelist_info[key] = {
            "exists": "1" if fpath.exists() else "0",
            "chain": " -> ".join(str(p) for p in chain_out),
            "parent": str(parent.resolve()) if parent else "",
            "include_kind": include_kind,
        }

    def parse_one(
        fpath: Path,
        *,
        content_base: Path,
        chain: List[Path],
        parent: Optional[Path] = None,
        include_kind: str = "",
    ) -> None:
        fpath = fpath.resolve()
        this_chain = chain + [fpath]
        register_filelist(
            fpath,
            chain=this_chain,
            parent=parent,
            include_kind=include_kind,
        )
        if fpath in seen_fl:
            return
        seen_fl.add(fpath)
        if on_progress:
            kind = include_kind or "top"
            parent_note = ""
            if parent is not None:
                parent_note = f" via {parent.name}"
            on_progress(f"filelist: reading {fpath.name} ({kind}{parent_note})")
        if not fpath.exists():
            result.errors.append(f"Filelist not found: {fpath}")
            if on_progress:
                on_progress(f"filelist: missing {fpath}")
            return
        base = content_base
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        for raw_line in text.splitlines():
            line = _strip_comments(raw_line)
            if not line or line.startswith("#"):
                continue
            if line.startswith("+incdir+"):
                body = line[len("+incdir+") :]
                parts = [body] if "+./" not in body and "+../" not in body else re.split(
                    r"(?=\+(?:\./|\.\./|/|[A-Za-z_$]))", body
                )
                for part in parts:
                    part = part.lstrip("+").strip()
                    if not part:
                        continue
                    add_incdir(resolve_path(part, base))
            elif line.startswith("+define+"):
                body = line[len("+define+") :]
                if "=" in body:
                    k, v = body.split("=", 1)
                else:
                    k, v = body, "1"
                result.defines[k.strip()] = v.strip()
            elif line.startswith("+libext+"):
                body = line[len("+libext+") :]
                for part in re.split(r"\+", body):
                    ext = part.strip()
                    if ext and not ext.startswith("."):
                        ext = "." + ext
                    if ext and ext not in result.libexts:
                        result.libexts.append(ext)
            elif (v_arg := _dash_switch_arg(line, "v")) is not None:
                vp = resolve_path(v_arg, base)
                if vp not in result.library_files:
                    result.library_files.append(vp)
            elif (y_arg := _dash_switch_arg(line, "y")) is not None:
                yp = resolve_path(y_arg, base)
                if yp not in result.library_dirs:
                    result.library_dirs.append(yp)
            elif line.startswith("+libdir+"):
                body = line[len("+libdir+") :]
                for part in re.split(r"\+", body):
                    part = part.lstrip("+").strip()
                    if part:
                        result.slang_options.append(
                            f"+libdir+{resolve_path(part, base)}"
                        )
            elif line.startswith("+librescan"):
                result.slang_options.append("+librescan")
            elif line.startswith("-sverilog") or line == "-sverilog":
                result.slang_options.append("-sverilog")
            elif line.startswith("-timescale="):
                result.slang_options.append(line)
            elif line.startswith("+ntb"):
                result.unsupported_options.append(line[:40])
            elif (top_mod := _word_switch_arg(line, "-topmodule")) is not None:
                if top_mod not in result.top_modules:
                    result.top_modules.append(top_mod)
            elif (top_name := _word_switch_arg(line, "-top")) is not None:
                if top_name not in result.top_modules:
                    result.top_modules.append(top_name)
            elif (work_lib := _word_switch_arg(line, "-worklib")) is not None:
                result.work_library = work_lib
            elif (work_name := _word_switch_arg(line, "-work")) is not None:
                result.work_library = work_name
            elif line.startswith("+top+"):
                body = line[len("+top+") :].strip()
                if body and body not in result.top_modules:
                    result.top_modules.append(body)
            elif (nested_hit := _nested_filelist_directive(line)) is not None:
                kind, nested = nested_hit
                locate_base = cwd if kind == "-F" else fpath.parent
                np = resolve_path(nested, locate_base)
                if on_progress:
                    on_progress(f"filelist: nested {kind} {nested!r} -> {np}")
                    for var in unexpanded_path_vars(str(np)):
                        on_progress(
                            f"filelist: WARNING unresolved env in {kind} path: {var}"
                        )
                chain_text = " -> ".join(str(p) for p in this_chain + [np])
                if _skip_nested_filelist(np, chain_text):
                    if on_progress:
                        on_progress(f"filelist: skip {np.name} (ignore-filelist)")
                    continue
                link_nested(fpath, np, kind)
                parse_one(
                    np,
                    content_base=np.parent if kind == "-f" else cwd,
                    chain=this_chain,
                    parent=fpath,
                    include_kind=kind,
                )
            else:
                tokens = line.split()
                if len(tokens) >= 2 and tokens[0] in ("-top", "-topmodule"):
                    if tokens[1] not in result.top_modules:
                        result.top_modules.append(tokens[1])
                elif len(tokens) >= 2 and tokens[0] in ("-work", "-worklib"):
                    result.work_library = tokens[1]
                for tok in tokens:
                    if tok.endswith((".v", ".sv", ".vh", ".svh")):
                        add_source(
                            resolve_path(tok, base),
                            via_filelist=fpath,
                            chain=this_chain,
                        )

    if on_progress:
        on_progress(f"filelist: expanding {top}")
        if normalize_filelist_token(raw_top) != str(top):
            on_progress(f"filelist: env-expand {raw_top!r} -> {top}")
        unresolved = unexpanded_path_vars(str(top))
        if unresolved:
            on_progress(
                "filelist: WARNING unresolved env in top path: "
                + ", ".join(unresolved)
            )
    parse_one(top.resolve(), content_base=top.parent, chain=[], include_kind="top")
    add_incdir(result.base_dir)
    result.index_cwd_used = cwd
    if on_progress:
        missing = sum(1 for e in result.errors if "not found" in e.lower())
        on_progress(
            "filelist: done — "
            f"{len(result.source_files)} sources, "
            f"{len(result.filelist_info)} .f files, "
            f"{len(result.incdirs)} incdirs, "
            f"{len(result.defines)} defines"
            + (f", {missing} missing" if missing else "")
        )
    return result


def build_slang_filelist_lines(fl: FilelistResult) -> List[str]:
    """Flatten :class:`FilelistResult` to lines pyslang ``processCommandFiles`` accepts."""
    from hierwalk.hch_compat.platform_paths import path_to_slang

    lines: List[str] = []
    if fl.libexts:
        lines.append("+libext+" + "+".join(fl.libexts))
    for inc in fl.incdirs:
        lines.append(f"+incdir+{path_to_slang(inc)}")
    for name, val in sorted(fl.defines.items()):
        lines.append(f"+define+{name}={val}" if val else f"+define+{name}")
    for ydir in fl.library_dirs:
        lines.append(f"-y {path_to_slang(ydir)}")
    for vfile in fl.library_files:
        lines.append(f"-v {path_to_slang(vfile)}")
    for opt in fl.slang_options:
        if opt:
            lines.append(opt)
    for src in fl.source_files:
        lines.append(path_to_slang(src))
    return lines


def _defines_cache_tag(defines: Mapping[str, str]) -> str:
    if not defines:
        return ""
    import hashlib
    import json

    blob = json.dumps(sorted(defines.items()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def slang_filelist_cache_path(
    fl: FilelistResult,
    cache_hint: Optional[Union[str, Path]] = None,
) -> Path:
    """Stable path for a cached pyslang filelist (under per-top work dir when set)."""
    tag = _defines_cache_tag(fl.defines)
    tag_part = f".{tag}" if tag else ""

    def _with_tag(path: Path) -> Path:
        if not tag:
            return path
        return path.parent / f"{path.stem}{tag_part}{path.suffix}"

    from hierwalk.cache import get_active_work_dir

    work_dir = get_active_work_dir()
    if work_dir is not None:
        return _with_tag(work_dir / "tmp" / f"{fl.top_path.stem}.hch_slang.f")
    if cache_hint:
        hint = Path(cache_hint)
        if hint.is_dir():
            return _with_tag(hint / "tmp" / f"{fl.top_path.stem}.hch_slang.f")
        if hint.suffix == ".db" or hint.name.endswith(".hch.db"):
            return _with_tag(hint.parent / f"{hint.name}.slang.f")
        return _with_tag(hint.resolve())
    return _with_tag((fl.base_dir / f".{fl.top_path.stem}.hch_slang.f").resolve())


def slang_filelist_is_stale(
    dest: Path,
    filelist_mtimes: Dict[str, float],
) -> bool:
    if not dest.exists():
        return True
    try:
        dest_mt = dest.stat().st_mtime
    except OSError:
        return True
    for path, mt in filelist_mtimes.items():
        p = Path(path)
        if not p.exists():
            return True
        try:
            if p.stat().st_mtime > dest_mt:
                return True
        except OSError:
            return True
    return False


def write_slang_filelist(
    fl: FilelistResult,
    dest: Optional[Union[str, Path]] = None,
) -> Path:
    """Write preprocessed filelist; return path."""
    body = "\n".join(build_slang_filelist_lines(fl)) + "\n"
    if dest is not None:
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        return out.resolve()
    from hierwalk.cache import get_active_work_dir

    work_dir = get_active_work_dir()
    if work_dir is not None:
        tmp = work_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f".{fl.top_path.stem}.hch_slang.f"
        out.write_text(body, encoding="utf-8")
        return out.resolve()
    fd, path = tempfile.mkstemp(suffix=".f", prefix="hch_slang_")
    os.close(fd)
    out = Path(path)
    out.write_text(body, encoding="utf-8")
    return out.resolve()


def write_slang_filelist_cached(
    fl: FilelistResult,
    *,
    index_cwd: Optional[Path] = None,
    cache_path: Optional[Union[str, Path]] = None,
    filelist_mtimes: Optional[Dict[str, float]] = None,
) -> Path:
    """Write or reuse preprocessed slang filelist when nested .f mtimes are unchanged."""
    from hierwalk.hch_compat.filelist_cache import collect_filelist_mtimes

    cwd = index_cwd or fl.index_cwd_used or fl.base_dir
    dest = slang_filelist_cache_path(fl, cache_path)
    mtimes = filelist_mtimes
    if mtimes is None:
        mtimes = collect_filelist_mtimes(fl.top_path, index_cwd=cwd)
    if not slang_filelist_is_stale(dest, mtimes):
        return dest.resolve()
    return write_slang_filelist(fl, dest)


@dataclass
class PreprocessedFilelist:
    expanded: FilelistResult
    slang_lines: List[str] = field(default_factory=list)
    slang_path: Optional[Path] = None


def preprocess_filelist_for_slang(
    top_filelist: Union[str, Path],
    env: Optional[Dict[str, str]] = None,
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    write_path: Optional[Union[str, Path]] = None,
) -> PreprocessedFilelist:
    """Expand -f/-F with EDA semantics and build a pyslang-safe filelist."""
    fl = expand_filelist(top_filelist, env, index_cwd=index_cwd)
    lines = build_slang_filelist_lines(fl)
    slang_path = (
        write_slang_filelist(fl, write_path)
        if fl.source_files or fl.library_files or fl.library_dirs
        else None
    )
    return PreprocessedFilelist(expanded=fl, slang_lines=lines, slang_path=slang_path)