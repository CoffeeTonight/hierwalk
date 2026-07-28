"""
Build essential SQLite index: files + module(name→file).

Standalone entry point for company-scale timing experiments (e.g. 13k RTL).

Default: NO ports table fill, NO hierarchy instances.
Ports are intentionally deferred (lazy later).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

from pyhirewalk.context import CompileContext, build_context
from pyhirewalk.filelist.paths import path_to_posix
from pyhirewalk.index.schema import SCHEMA_SQL, SCHEMA_VERSION

OnProgress = Callable[[str], None]


@dataclass
class BuildDbResult:
    """Outcome of :func:`build_essential_db`."""

    db_path: Path
    context_id: str
    n_files: int
    n_modules: int
    n_unique_module_names: int
    timings: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pyslang_version: str = ""
    flat_filelist: Optional[Path] = None

    def summary(self) -> Dict[str, object]:
        return {
            "db_path": str(self.db_path),
            "context_id": self.context_id,
            "n_files": self.n_files,
            "n_modules": self.n_modules,
            "n_unique_module_names": self.n_unique_module_names,
            "timings_sec": dict(self.timings),
            "total_sec": self.timings.get("total", sum(self.timings.values())),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "pyslang_version": self.pyslang_version,
            "flat_filelist": str(self.flat_filelist) if self.flat_filelist else None,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phase(timings: Dict[str, float], name: str, t0: float) -> float:
    timings[name] = time.perf_counter() - t0
    return time.perf_counter()


def _stat_file(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        st = path.stat()
        return int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return None, None


def _ensure_top_in_flat(flat: Path, top: Optional[str]) -> None:
    if not top:
        return
    text = flat.read_text(encoding="utf-8")
    if any(line.strip().startswith("-top") for line in text.splitlines()):
        return
    flat.write_text(f"-top {top}\n{text}", encoding="utf-8")


def _collect_definitions(
    flat: Path,
    *,
    on_progress: Optional[OnProgress],
) -> tuple[List[tuple[str, str, str]], str, List[str]]:
    """
    Returns (rows, pyslang_version, errors).

    rows: list of (module_name, kind, absolute_path)
    """
    try:
        import pyslang
    except ImportError as e:
        raise RuntimeError(
            "pyslang is required for build_essential_db. "
            "Install with: pip install pyslang"
        ) from e

    ver = ""
    try:
        ver = str(pyslang.VersionInfo.get() if hasattr(pyslang, "VersionInfo") else "")
    except Exception:
        ver = getattr(pyslang, "__version__", "") or "unknown"
    if not ver and hasattr(pyslang, "VersionInfo"):
        try:
            vi = pyslang.VersionInfo
            ver = f"{vi.major}.{vi.minor}.{vi.patch}" if hasattr(vi, "major") else str(vi)
        except Exception:
            ver = "pyslang"

    errors: List[str] = []
    if on_progress:
        on_progress("pyslang: processCommandFiles + parseAllSources …")

    driver = pyslang.driver.Driver()
    driver.addStandardArgs()
    if not driver.processCommandFiles(str(flat), False, False):
        errors.append("pyslang processCommandFiles failed")
        return [], ver, errors
    if not driver.processOptions():
        errors.append("pyslang processOptions failed")
        return [], ver, errors
    if not driver.parseAllSources():
        errors.append("pyslang parseAllSources failed")
        return [], ver, errors

    if on_progress:
        on_progress("pyslang: createCompilation + getDefinitions …")

    comp = driver.createCompilation()
    sm = driver.sourceManager
    rows: List[tuple[str, str, str]] = []

    # Surface parse/semantic issues as warnings (non-fatal for indexing)
    try:
        diags = list(comp.getAllDiagnostics())
        fatal = bool(getattr(comp, "hasFatalErrors", False))
        if fatal:
            errors.append(f"pyslang reported fatal diagnostics ({len(diags)} total)")
        for d in diags[:30]:
            errors.append(f"diag: {d}")
    except Exception as e:
        errors.append(f"diagnostics read failed: {e}")

    try:
        definitions = list(comp.getDefinitions())
    except Exception as e:
        errors.append(f"getDefinitions failed: {e}")
        return [], ver, errors

    for d in definitions:
        name = getattr(d, "name", None) or ""
        if not name:
            continue
        kind = str(getattr(d, "kind", "Definition")).split(".")[-1]
        # Normalize kind: Definition → try syntax or definitionKind
        kind_s = kind
        for attr in ("definitionKind", "kind"):
            if hasattr(d, attr):
                kind_s = str(getattr(d, attr)).split(".")[-1]
                break
        loc = getattr(d, "location", None)
        path = ""
        if loc is not None:
            try:
                path = path_to_posix(Path(sm.getFileName(loc)))
            except Exception:
                try:
                    path = str(sm.getFileName(loc))
                except Exception:
                    path = ""
        if not path:
            continue
        rows.append((name, kind_s, path))

    if on_progress:
        on_progress(f"pyslang: {len(rows)} definitions")
    return rows, ver, errors


def build_essential_db(
    filelist: Union[str, Path],
    db_path: Union[str, Path],
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    top: Optional[str] = None,
    extra_defines: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    on_progress: Optional[OnProgress] = None,
    defer_source_exists: bool = False,
) -> BuildDbResult:
    """
    Expand filelist, parse with pyslang, write essential SQLite DB.

    Tables filled: ``meta``, ``files``, ``modules``, ``build_timing``.
    ``ports`` left empty (lazy later — not needed for full-file inventory).

    Parameters
    ----------
    filelist
        Top-level EDA ``.f``.
    db_path
        Output ``.sqlite`` path (created/overwritten).
    index_cwd
        Run directory for ``-F`` semantics.
    top
        Optional top module (written into flat slang filelist).
    work_dir
        Where to place intermediate flat ``.f`` (default: next to db).
    """
    t_all = time.perf_counter()
    timings: Dict[str, float] = {}
    warnings: List[str] = []
    errors: List[str] = []

    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).resolve() if work_dir else db_path.parent
    work.mkdir(parents=True, exist_ok=True)

    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # --- 1) context / filelist ---
    prog("phase: filelist expand")
    t0 = time.perf_counter()
    ctx: CompileContext = build_context(
        filelist,
        index_cwd=index_cwd,
        extra_defines=extra_defines,
        env=env,
        top=top,
        defer_source_exists=defer_source_exists,
    )
    t0 = _phase(timings, "filelist_expand", t0)
    if ctx.errors:
        warnings.extend(f"filelist: {e}" for e in ctx.errors)

    # --- 2) flat slang filelist ---
    prog("phase: write flat slang filelist")
    flat = work / f"{ctx.context_id}.flat.slang.f"
    ctx.write_slang_filelist(flat)
    top_name = top or (ctx.top_modules[0] if ctx.top_modules else "")
    _ensure_top_in_flat(flat, top_name or None)
    t0 = _phase(timings, "write_flat_f", t0)

    # --- 3) pyslang definitions ---
    prog("phase: pyslang parse + definitions")
    try:
        def_rows, pyslang_ver, slang_errs = _collect_definitions(
            flat, on_progress=on_progress
        )
    except RuntimeError as e:
        timings["pyslang_definitions"] = time.perf_counter() - t0
        timings["total"] = time.perf_counter() - t_all
        return BuildDbResult(
            db_path=db_path,
            context_id=ctx.context_id,
            n_files=0,
            n_modules=0,
            n_unique_module_names=0,
            timings=timings,
            errors=[str(e)],
            warnings=warnings,
            flat_filelist=flat,
        )
    errors.extend(slang_errs)
    t0 = _phase(timings, "pyslang_definitions", t0)

    # --- 4) sqlite ---
    prog("phase: sqlite write")
    if db_path.exists():
        db_path.unlink()

    path_to_id: Dict[str, int] = {}
    n_mod = 0
    names: set[str] = set()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO meta(
              context_id, top, top_filelist, index_cwd, defines_json,
              created_at, pyslang_version, schema_version, notes_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                ctx.context_id,
                top_name,
                path_to_posix(ctx.top_filelist),
                path_to_posix(ctx.index_cwd),
                json.dumps(ctx.defines, sort_keys=True),
                _now_iso(),
                pyslang_ver,
                SCHEMA_VERSION,
                json.dumps(
                    {
                        "ports_filled": False,
                        "instances_filled": False,
                        "n_filelist_errors": len(ctx.errors),
                    }
                ),
            ),
        )

        file_id = 0

        def add_file(path_str: str, role: str) -> int:
            nonlocal file_id
            key = path_to_posix(path_str)
            if key in path_to_id:
                return path_to_id[key]
            file_id += 1
            p = Path(key)
            mt, sz = _stat_file(p)
            conn.execute(
                """
                INSERT INTO files(file_id, context_id, path, role, mtime_ns, size)
                VALUES (?,?,?,?,?,?)
                """,
                (file_id, ctx.context_id, key, role, mt, sz),
            )
            path_to_id[key] = file_id
            return file_id

        for src in ctx.source_files:
            add_file(path_to_posix(src), "listed")
        for lib in ctx.library_files:
            add_file(path_to_posix(lib), "library")

        for name, kind, fpath in def_rows:
            key = path_to_posix(fpath)
            role = "listed" if key in path_to_id else "definition"
            fid = add_file(fpath, role)
            conn.execute(
                """
                INSERT OR IGNORE INTO modules(context_id, name, kind, file_id)
                VALUES (?,?,?,?)
                """,
                (ctx.context_id, name, kind, fid),
            )
            n_mod += 1
            names.add(name)

        conn.commit()
    finally:
        conn.close()

    _phase(timings, "sqlite_write", t0)
    timings["total"] = time.perf_counter() - t_all

    conn = sqlite3.connect(str(db_path))
    try:
        for phase, sec in timings.items():
            conn.execute(
                "INSERT OR REPLACE INTO build_timing(context_id, phase, seconds) "
                "VALUES (?,?,?)",
                (ctx.context_id, phase, sec),
            )
        conn.commit()
    finally:
        conn.close()

    prog(
        f"done: files={len(path_to_id)} modules={n_mod} "
        f"unique_names={len(names)} total={timings['total']:.3f}s"
    )

    return BuildDbResult(
        db_path=db_path,
        context_id=ctx.context_id,
        n_files=len(path_to_id),
        n_modules=n_mod,
        n_unique_module_names=len(names),
        timings=timings,
        errors=errors,
        warnings=warnings,
        pyslang_version=pyslang_ver,
        flat_filelist=flat,
    )


def build_essential_db_from_context(
    ctx: CompileContext,
    db_path: Union[str, Path],
    *,
    top: Optional[str] = None,
    work_dir: Optional[Union[str, Path]] = None,
    on_progress: Optional[OnProgress] = None,
) -> BuildDbResult:
    """Same as :func:`build_essential_db` but reuses an existing :class:`CompileContext`."""
    return build_essential_db(
        ctx.top_filelist,
        db_path,
        index_cwd=ctx.index_cwd,
        top=top or (ctx.top_modules[0] if ctx.top_modules else None),
        extra_defines=ctx.extra_defines or None,
        work_dir=work_dir,
        on_progress=on_progress,
    )
