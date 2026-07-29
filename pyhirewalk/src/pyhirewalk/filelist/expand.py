"""
Expand Verilog/SystemVerilog filelists (-f / -F) into a flat compile view.

VCS semantics (intentionally preserved from battle-tested EDA usage):

  -f nested.f  — locate nested.f relative to the containing .f directory;
                 paths *inside* nested.f are relative to nested.f's directory.
  -F nested.f  — locate nested.f relative to index_cwd (EDA run directory);
                 paths *inside* nested.f are relative to index_cwd.

Environment variables (how EDA uses them in .f)
-----------------------------------------------
The simulator process inherits shell exports (``PROJ``, ``RTL_ROOT``, …).
Every path-like token in the filelist is env-expanded **before** joining with
the content base:

  -f $PROJ/ip/uart/files.f
  +incdir+$RTL_ROOT/include+$VIP/include
  -y $TECH_LIB/stdlib
  $RTL_ROOT/top.sv

Syntax: ``$NAME`` / ``${NAME}`` (see :mod:`pyhirewalk.filelist.envexpand`).
Run-JSON ``env`` injects the same names when there is no login shell.

pyslang does not implement -F; callers should flatten via
:func:`build_slang_filelist_lines` / :func:`write_slang_filelist`.

This module is self-contained (no hierwalk imports).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Union

from pyhirewalk.filelist.cwd import resolve_index_cwd
from pyhirewalk.filelist.envexpand import build_env_map, expand_eda_env
from pyhirewalk.filelist.paths import (
    normalize_filelist_token,
    path_to_slang,
    resolve_path as resolve_abs,
)

OnProgress = Callable[[str], None]

_SOURCE_SUFFIXES = (".v", ".sv", ".vh", ".svh")


@dataclass
class FilelistResult:
    """Expanded top-level filelist (absolute paths where possible)."""

    top_path: Path
    base_dir: Path
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
    unresolved_env: List[str] = field(default_factory=list)
    index_cwd_used: Optional[Path] = None
    # Provenance: which listing file introduced each source / nested .f
    source_via_filelist: Dict[Path, Path] = field(default_factory=dict)
    source_filelist_chain: Dict[Path, str] = field(default_factory=dict)
    filelist_info: Dict[Path, Dict[str, str]] = field(default_factory=dict)
    filelist_children: Dict[Path, List[Path]] = field(default_factory=dict)
    filelist_edges: List[tuple[Path, Path, str]] = field(default_factory=list)


def _strip_comments(line: str) -> str:
    line = re.sub(r"/\*.*?\*/", "", line)
    if "//" in line:
        line = line.split("//", 1)[0]
    return line.strip()


def _matches_ignore(path: Path, chain_text: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    name = path.name
    full = path.as_posix()
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p.startswith("filelist:"):
            p = p.split(":", 1)[1].strip()
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(full, p):
            return True
        if chain_text and (p in chain_text or fnmatch.fnmatch(chain_text, f"*{p}*")):
            return True
    return False


def expand_filelist(
    top_filelist: Union[str, Path],
    env: Optional[Mapping[str, str]] = None,
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    on_progress: Optional[OnProgress] = None,
    ignore_filelist_patterns: Optional[Sequence[str]] = None,
    defer_source_exists: bool = False,
) -> FilelistResult:
    """
    Expand a top ``.f`` into :class:`FilelistResult`.

    ``index_cwd`` is the directory tools use for ``-F`` (EDA run directory).
    ``env`` is merged over ``os.environ`` for ``$VAR`` / ``${VAR}`` in path tokens
    (JSON run ``env`` or explicit map — same role as shell ``export`` before vcs).
    """
    top = resolve_abs(top_filelist)
    env_map = build_env_map(env)
    cwd = resolve_index_cwd(top, index_cwd, env_map)
    result = FilelistResult(top_path=top, base_dir=top.parent)
    seen_fl: Set[Path] = set()
    seen_src: Set[Path] = set()
    ignore_fl = list(ignore_filelist_patterns or ())
    unresolved_seen: Set[str] = set()

    def expand_env(s: str, *, where: str = "") -> str:
        out, missing = expand_eda_env(s, env_map, keep_unset=True)
        for name in missing:
            if name not in unresolved_seen:
                unresolved_seen.add(name)
                result.unresolved_env.append(name)
                loc = f" ({where})" if where else ""
                result.errors.append(
                    f"Unset environment variable ${{{name}}} in filelist{loc}: {s!r}"
                )
        return out

    def resolve_path(raw: str, base: Path, *, where: str = "") -> Path:
        """Env-expand token, then resolve relative to content base (EDA order)."""
        raw = expand_env(normalize_filelist_token(raw), where=where)
        p = Path(raw)
        if not p.is_absolute():
            p = base / p
        return resolve_abs(p)

    def add_source(sp: Path, *, via_filelist: Path, chain: List[Path]) -> None:
        if sp in seen_src:
            return
        seen_src.add(sp)
        result.source_via_filelist[sp] = via_filelist
        result.source_filelist_chain[sp] = " -> ".join(str(p) for p in chain)
        if defer_source_exists or sp.exists():
            result.source_files.append(sp)
        else:
            result.errors.append(f"Source not found: {sp}")

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
            parent_note = f" via {parent.name}" if parent is not None else ""
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

            where = f"{fpath.name}"

            if line.startswith("+incdir+"):
                # EDA: +incdir+dir1+dir2+…  ($VAR may appear in any dir)
                # Expand env first, then split on '+' path separators.
                # (Old +./-only split failed after expand: …/include+/abs/vip)
                body = expand_env(line[len("+incdir+") :], where=f"{where}: +incdir+")
                parts = [p.strip() for p in body.split("+") if p.strip()]
                for part in parts:
                    p = Path(part)
                    if not p.is_absolute():
                        p = base / p
                    add_incdir(resolve_abs(p))

            elif line.startswith("+define+"):
                # Macro names usually plain; values may embed $PROJ (rare but legal)
                body = expand_env(line[len("+define+") :], where=f"{where}: +define+")
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

            elif line.startswith("-v "):
                vp = resolve_path(line[3:].strip(), base, where=f"{where}: -v")
                if vp not in result.library_files:
                    result.library_files.append(vp)

            elif line.startswith("-y "):
                yp = resolve_path(line[3:].strip(), base, where=f"{where}: -y")
                if yp not in result.library_dirs:
                    result.library_dirs.append(yp)

            elif line.startswith("+libdir+"):
                body = expand_env(line[len("+libdir+") :], where=f"{where}: +libdir+")
                for part in re.split(r"\+", body):
                    part = part.lstrip("+").strip()
                    if part:
                        p = Path(part)
                        if not p.is_absolute():
                            p = base / p
                        result.slang_options.append(
                            f"+libdir+{path_to_slang(resolve_abs(p))}"
                        )

            elif line.startswith("+librescan"):
                result.slang_options.append("+librescan")

            elif line.startswith("-sverilog") or line == "-sverilog":
                result.slang_options.append("-sverilog")

            elif line.startswith("-timescale="):
                result.slang_options.append(line)

            elif line.startswith("+ntb"):
                result.unsupported_options.append(line[:40])

            elif line.startswith("-top ") or line.startswith("-topmodule "):
                top_name = line.split(maxsplit=1)[1].strip() if " " in line else ""
                if top_name and top_name not in result.top_modules:
                    result.top_modules.append(top_name)

            elif line.startswith("-work ") or line.startswith("-worklib "):
                result.work_library = line.split(maxsplit=1)[1].strip()

            elif line.startswith("+top+"):
                body = line[len("+top+") :].strip()
                if body and body not in result.top_modules:
                    result.top_modules.append(body)

            elif line.startswith("-f "):
                nested = line[3:].strip()
                np = resolve_path(nested, fpath.parent, where=f"{where}: -f")
                chain_text = " -> ".join(str(p) for p in this_chain + [np])
                if _matches_ignore(np, chain_text, ignore_fl):
                    if on_progress:
                        on_progress(f"filelist: skip {np.name} (ignore)")
                    continue
                link_nested(fpath, np, "-f")
                parse_one(
                    np,
                    content_base=np.parent,
                    chain=this_chain,
                    parent=fpath,
                    include_kind="-f",
                )

            elif line.startswith("-F "):
                nested = line[3:].strip()
                # -F: nested list path relative to index_cwd AFTER env expand
                np = resolve_path(nested, cwd, where=f"{where}: -F")
                chain_text = " -> ".join(str(p) for p in this_chain + [np])
                if _matches_ignore(np, chain_text, ignore_fl):
                    if on_progress:
                        on_progress(f"filelist: skip {np.name} (ignore)")
                    continue
                link_nested(fpath, np, "-F")
                parse_one(
                    np,
                    content_base=cwd,
                    chain=this_chain,
                    parent=fpath,
                    include_kind="-F",
                )

            else:
                tokens = line.split()
                if len(tokens) >= 2 and tokens[0] in ("-top", "-topmodule"):
                    if tokens[1] not in result.top_modules:
                        result.top_modules.append(tokens[1])
                elif len(tokens) >= 2 and tokens[0] in ("-work", "-worklib"):
                    result.work_library = tokens[1]
                for tok in tokens:
                    # Source may be $RTL_ROOT/top.sv — expand then check suffix
                    expanded_tok = expand_env(
                        normalize_filelist_token(tok), where=f"{where}: source"
                    )
                    if expanded_tok.endswith(_SOURCE_SUFFIXES) or tok.endswith(
                        _SOURCE_SUFFIXES
                    ):
                        p = Path(expanded_tok)
                        if not p.is_absolute():
                            p = base / p
                        add_source(
                            resolve_abs(p),
                            via_filelist=fpath,
                            chain=this_chain,
                        )

    if on_progress:
        on_progress(f"filelist: expanding {top.name}")
    parse_one(top.resolve(), content_base=top.parent, chain=[], include_kind="top")
    add_incdir(result.base_dir)
    result.index_cwd_used = cwd
    if on_progress:
        missing = sum(1 for e in result.errors if "not found" in e.lower())
        unset = len(result.unresolved_env)
        on_progress(
            "filelist: done — "
            f"{len(result.source_files)} sources, "
            f"{len(result.filelist_info)} .f files, "
            f"{len(result.incdirs)} incdirs, "
            f"{len(result.defines)} defines"
            + (f", {missing} missing" if missing else "")
            + (f", {unset} unset env" if unset else "")
        )
    return result


def build_slang_filelist_lines(fl: FilelistResult) -> List[str]:
    """Flatten :class:`FilelistResult` to lines pyslang accepts (no nested -f/-F)."""
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


def write_slang_filelist(
    fl: FilelistResult,
    dest: Union[str, Path],
) -> Path:
    """Write a flat slang-safe filelist to ``dest`` and return its path."""
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(build_slang_filelist_lines(fl)) + "\n"
    out.write_text(body, encoding="utf-8")
    return out.resolve()
