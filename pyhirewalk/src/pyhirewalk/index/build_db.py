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
    n_filelists: int = 0  # parent + nested .f seen during expand
    n_rtl_sources: int = 0  # source_files from filelist expand
    timings: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pyslang_version: str = ""
    flat_filelist: Optional[Path] = None
    modules_json: Optional[Path] = None  # modulename → [filepath, …]

    def summary(self) -> Dict[str, object]:
        return {
            "db_path": str(self.db_path),
            "db_format": "sqlite",
            "modules_json": str(self.modules_json) if self.modules_json else None,
            "context_id": self.context_id,
            "n_files": self.n_files,
            "n_modules": self.n_modules,
            "n_unique_module_names": self.n_unique_module_names,
            "n_filelists": self.n_filelists,
            "n_rtl_sources": self.n_rtl_sources,
            "timings_sec": dict(self.timings),
            "total_sec": self.timings.get("total", sum(self.timings.values())),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "pyslang_version": self.pyslang_version,
            "flat_filelist": str(self.flat_filelist) if self.flat_filelist else None,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts() -> str:
    """Local wall-clock for human logs."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def _build_essential_db_impl(
    filelist: Union[str, Path],
    db_path: Union[str, Path],
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    top: Optional[str] = None,
    extra_defines: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    mode: str = "fast",
    scan_workers: int = 8,
    modules_json: Optional[Union[str, Path]] = None,
    write_sqlite: bool = True,
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
        Output ``.sqlite`` path (created/overwritten if write_sqlite).
    modules_json
        Output modulename→filepath JSON (default: ``<db_stem>.modules.json`` next to db).
    write_sqlite
        If False, only write modules JSON (skip SQLite).
    index_cwd
        Run directory for ``-F`` semantics.
    top
        Optional top module (written into flat slang filelist).
    work_dir
        Where to place intermediate flat ``.f`` (default: next to db).
    mode
        ``fast`` (default): parallel text scan for module/interface/package names.
        ``pyslang``: full parse — often tens of minutes on large RTL.
    scan_workers
        Thread count for ``fast`` mode.
    """
    t_all = time.perf_counter()
    timings: Dict[str, float] = {}
    warnings: List[str] = []
    errors: List[str] = []
    mode_norm = (mode or "fast").strip().lower()
    if mode_norm not in ("fast", "pyslang"):
        raise ValueError(f"unknown build_db mode={mode!r} (use fast|pyslang)")

    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).resolve() if work_dir else db_path.parent
    work.mkdir(parents=True, exist_ok=True)
    map_path = (
        Path(modules_json).resolve()
        if modules_json
        else (db_path.parent / f"{db_path.stem}.modules.json")
    )

    def prog(msg: str) -> None:
        """Always timestamped; wall clock + optional elapsed-from-start."""
        if on_progress:
            elapsed = time.perf_counter() - t_all
            on_progress(f"[{_ts()}] (+{elapsed:8.3f}s) {msg}")

    def end_phase(name: str, t0: float, detail: str = "") -> float:
        t0 = _phase(timings, name, t0)
        sec = timings[name]
        extra = f" — {detail}" if detail else ""
        prog(f"phase done: {name}  took {sec:.3f}s{extra}")
        return t0

    prog(f"build_db START  out={db_path}  format=SQLite  mode={mode_norm}")
    prog(f"  parent_filelist={filelist}")
    if top:
        prog(f"  top={top}")
    if mode_norm == "pyslang":
        prog(
            "  note: mode=pyslang parses ALL listed RTL — large designs often "
            "need 10–40+ min; use mode=fast for name→file catalog"
        )

    # --- 1) context / filelist ---
    prog("phase start: filelist_expand")
    t0 = time.perf_counter()
    ctx: CompileContext = build_context(
        filelist,
        index_cwd=index_cwd,
        extra_defines=extra_defines,
        env=env,
        top=top,
        defer_source_exists=defer_source_exists,
        on_progress=prog,
    )
    n_filelists = 0
    if ctx.raw is not None:
        n_filelists = len(getattr(ctx.raw, "filelist_info", {}) or {})
    if n_filelists == 0:
        # parent only + nested edges as lower bound
        n_filelists = 1 + len(ctx.filelist_edges)
    n_rtl_sources = len(ctx.source_files)

    t0 = end_phase(
        "filelist_expand",
        t0,
        f"filelists={n_filelists} rtl_sources={n_rtl_sources} "
        f"defines={len(ctx.defines)} filelist_errors={len(ctx.errors)}",
    )
    prog(
        f"filelist inventory: n_filelists={n_filelists} "
        f"(parent + nested .f)  n_rtl_sources={n_rtl_sources}"
    )
    if ctx.errors:
        warnings.extend(f"filelist: {e}" for e in ctx.errors)

    # --- 2) flat slang filelist (still useful for later pyslang scoped work) ---
    prog("phase start: write_flat_f")
    flat = work / f"{ctx.context_id}.flat.slang.f"
    ctx.write_slang_filelist(flat)
    top_name = top or (ctx.top_modules[0] if ctx.top_modules else "")
    _ensure_top_in_flat(flat, top_name or None)
    t0 = end_phase("write_flat_f", t0, f"flat={flat.name}")

    # --- 3) definitions: fast scan (default) or full pyslang ---
    def_rows: List[tuple[str, str, str]] = []
    pyslang_ver = ""
    if mode_norm == "fast":
        from pyhirewalk.index.scan_defs import collect_definitions_fast

        prog("phase start: definitions_fast")
        t0 = time.perf_counter()
        def_rows, scan_errs = collect_definitions_fast(
            list(ctx.source_files),
            on_progress=prog,
            workers=scan_workers,
        )
        errors.extend(scan_errs)
        t0 = end_phase(
            "definitions_fast",
            t0,
            f"definitions={len(def_rows)} workers={scan_workers}",
        )
        # alias for timing table compatibility
        timings["definitions"] = timings.get("definitions_fast", 0.0)
    else:
        prog("phase start: pyslang_definitions")
        t0 = time.perf_counter()
        try:
            def_rows, pyslang_ver, slang_errs = _collect_definitions(
                flat, on_progress=prog
            )
        except RuntimeError as e:
            timings["pyslang_definitions"] = time.perf_counter() - t0
            timings["total"] = time.perf_counter() - t_all
            prog(
                f"phase FAIL: pyslang_definitions  "
                f"took {timings['pyslang_definitions']:.3f}s"
            )
            prog(
                f"build_db TOTAL wall time: {timings['total']:.3f}s  "
                f"({timings['total'] / 60.0:.2f} min)  FAILED"
            )
            return BuildDbResult(
                db_path=db_path,
                context_id=ctx.context_id,
                n_files=0,
                n_modules=0,
                n_unique_module_names=0,
                n_filelists=n_filelists,
                n_rtl_sources=n_rtl_sources,
                timings=timings,
                errors=[str(e)],
                warnings=warnings,
                flat_filelist=flat,
            )
        errors.extend(slang_errs)
        t0 = end_phase(
            "pyslang_definitions",
            t0,
            f"definitions={len(def_rows)} pyslang={pyslang_ver or '?'}",
        )
        timings["definitions"] = timings.get("pyslang_definitions", 0.0)

    # --- 4a) modules JSON (modulename → [filepath, …]) ---
    prog("phase start: write_modules_json")
    t0 = time.perf_counter()
    mod_map: Dict[str, List[str]] = {}
    names: set[str] = set()
    for name, kind, fpath in def_rows:
        key = path_to_posix(fpath)
        mod_map.setdefault(name, [])
        if key not in mod_map[name]:
            mod_map[name].append(key)
        names.add(name)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_doc = {
        "schema_version": 1,
        "meta": {
            "context_id": ctx.context_id,
            "top": top_name,
            "top_filelist": path_to_posix(ctx.top_filelist),
            "index_cwd": path_to_posix(ctx.index_cwd),
            "defines": dict(ctx.defines),
            "mode": mode_norm,
            "created_at": _now_iso(),
            "n_filelists": n_filelists,
            "n_rtl_sources": n_rtl_sources,
            "n_modules": len(names),
            "n_def_rows": len(def_rows),
        },
        "modules": mod_map,
    }
    map_path.write_text(json.dumps(map_doc, indent=2) + "\n", encoding="utf-8")
    t0 = end_phase(
        "write_modules_json",
        t0,
        f"path={map_path} unique_names={len(names)}",
    )
    prog(f"modules_json: {map_path}")

    # --- 4b) optional sqlite ---
    path_to_id: Dict[str, int] = {}
    n_mod = len(def_rows)
    if write_sqlite:
        prog("phase start: sqlite_write")
        t0 = time.perf_counter()
        if db_path.exists():
            db_path.unlink()

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
                            "n_filelists": n_filelists,
                            "n_rtl_sources": n_rtl_sources,
                            "mode": mode_norm,
                            "scan_workers": scan_workers if mode_norm == "fast" else None,
                            "modules_json": path_to_posix(map_path),
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

            conn.commit()
        finally:
            conn.close()

        end_phase(
            "sqlite_write",
            t0,
            f"db_files={len(path_to_id)} modules={n_mod} unique_names={len(names)}",
        )

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
    else:
        prog("sqlite_write: skipped (--no-sqlite)")
        for p in mod_map.values():
            for fp in p:
                path_to_id[fp] = 1

    timings["total"] = time.perf_counter() - t_all

    # Phase breakdown + wall-clock total (always log — primary user ask)
    prog("----- inventory (final) -----")
    prog(f"  n_filelists     = {n_filelists}   # parent + nested .f expanded")
    prog(f"  n_rtl_sources   = {n_rtl_sources}   # RTL paths from filelist")
    prog(f"  n_db_files      = {len(path_to_id)}   # rows in files table")
    prog(f"  n_modules       = {n_mod}   # definition rows (module→file)")
    prog(f"  n_unique_names  = {len(names)}")
    prog(f"  modules_json    = {map_path}")
    prog("----- timing breakdown -----")
    order = (
        "filelist_expand",
        "write_flat_f",
        "definitions_fast",
        "pyslang_definitions",
        "definitions",
        "write_modules_json",
        "sqlite_write",
        "total",
    )
    for key in order:
        if key in timings:
            prog(f"  {key:22s}  {timings[key]:10.3f} s")
    for key, sec in timings.items():
        if key not in order:
            prog(f"  {key:22s}  {sec:10.3f} s")

    total = timings["total"]
    prog(
        f"build_db TOTAL wall time: {total:.3f}s  ({total / 60.0:.2f} min)  "
        f"filelists={n_filelists} files={len(path_to_id)} modules={n_mod}  "
        f"map={map_path}"
    )
    prog("build_db END OK")

    return BuildDbResult(
        db_path=db_path,
        context_id=ctx.context_id,
        n_files=len(path_to_id),
        n_modules=n_mod,
        n_unique_module_names=len(names),
        n_filelists=n_filelists,
        n_rtl_sources=n_rtl_sources,
        timings=timings,
        errors=errors,
        warnings=warnings,
        pyslang_version=pyslang_ver,
        flat_filelist=flat,
        modules_json=map_path,
    )



class BuildDb:
    """Class API for essential module-map / SQLite build (same as build_essential_db)."""

    def __init__(
        self,
        filelist: Union[str, Path],
        db_path: Union[str, Path],
        *,
        index_cwd: Optional[Union[str, Path]] = None,
        top: Optional[str] = None,
        extra_defines: Optional[Mapping[str, str]] = None,
        env: Optional[Mapping[str, str]] = None,
        work_dir: Optional[Union[str, Path]] = None,
        mode: str = "fast",
        scan_workers: int = 8,
        modules_json: Optional[Union[str, Path]] = None,
        write_sqlite: bool = True,
        on_progress: Optional[OnProgress] = None,
        defer_source_exists: bool = False,
    ) -> None:
        self.filelist = filelist
        self.db_path = db_path
        self.index_cwd = index_cwd
        self.top = top
        self.extra_defines = extra_defines
        self.env = env
        self.work_dir = work_dir
        self.mode = mode
        self.scan_workers = scan_workers
        self.modules_json = modules_json
        self.write_sqlite = write_sqlite
        self.on_progress = on_progress
        self.defer_source_exists = defer_source_exists
        self.result: Optional[BuildDbResult] = None

    def run(self) -> BuildDbResult:
        self.result = _build_essential_db_impl(
            self.filelist,
            self.db_path,
            index_cwd=self.index_cwd,
            top=self.top,
            extra_defines=self.extra_defines,
            env=self.env,
            work_dir=self.work_dir,
            mode=self.mode,
            scan_workers=self.scan_workers,
            modules_json=self.modules_json,
            write_sqlite=self.write_sqlite,
            on_progress=self.on_progress,
            defer_source_exists=self.defer_source_exists,
        )
        return self.result


def build_essential_db(
    filelist: Union[str, Path],
    db_path: Union[str, Path],
    *,
    index_cwd: Optional[Union[str, Path]] = None,
    top: Optional[str] = None,
    extra_defines: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    mode: str = "fast",
    scan_workers: int = 8,
    modules_json: Optional[Union[str, Path]] = None,
    write_sqlite: bool = True,
    on_progress: Optional[OnProgress] = None,
    defer_source_exists: bool = False,
) -> BuildDbResult:
    """Build essential index; delegates to :class:`BuildDb`."""
    return BuildDb(
        filelist,
        db_path,
        index_cwd=index_cwd,
        top=top,
        extra_defines=extra_defines,
        env=env,
        work_dir=work_dir,
        mode=mode,
        scan_workers=scan_workers,
        modules_json=modules_json,
        write_sqlite=write_sqlite,
        on_progress=on_progress,
        defer_source_exists=defer_source_exists,
    ).run()



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
