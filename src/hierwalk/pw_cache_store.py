"""Path-walk disk cache backends: per-file pickle sidecars or SQLite."""

from __future__ import annotations

import pickle
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from hierwalk.models import ModuleRecord

PW_CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FileRegexCacheEntry:
    content_digest: str
    module_names: Tuple[str, ...]


@dataclass(frozen=True)
class FileValidatedCacheEntry:
    content_digest: str
    defines_digest: str
    modules: Tuple[Tuple[str, ModuleRecord], ...]
    include_closure_digest: str = ""
    preprocess_tag: str = ""


@dataclass(frozen=True)
class FilePreprocessedCacheEntry:
    content_digest: str
    defines_digest: str
    include_closure_digest: str
    text: str
    preprocess_tag: str = ""


def file_cache_token(path: str) -> str:
    import hashlib

    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:20]


def _pickle_regex_sidecar_path(cache_root: Path, path: str) -> Path:
    return cache_root / "regex" / f"{file_cache_token(path)}.pkl"


def _pickle_validated_sidecar_path(
    cache_root: Path,
    path: str,
    *,
    defines_digest: str,
    preprocess_tag: str,
) -> Path:
    suffix = f"_{preprocess_tag}" if preprocess_tag else ""
    return cache_root / "validated" / f"{file_cache_token(path)}_{defines_digest}{suffix}.pkl"


def _pickle_preprocessed_sidecar_path(
    cache_root: Path,
    path: str,
    *,
    defines_digest: str,
    include_closure_digest: str,
    preprocess_tag: str,
) -> Path:
    suffix = f"_{preprocess_tag}" if preprocess_tag else ""
    return (
        cache_root
        / "preprocessed"
        / f"{file_cache_token(path)}_{defines_digest}_{include_closure_digest}{suffix}.pkl"
    )


def _atomic_pickle_write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def _load_pickle(path: Path, expected_type: type) -> Optional[object]:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            obj = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError, ValueError):
        return None
    if not isinstance(obj, expected_type):
        return None
    return obj


class PathWalkCacheStore(ABC):
    @abstractmethod
    def load_regex(self, path: str, *, content_digest: str) -> Optional[FileRegexCacheEntry]:
        ...

    @abstractmethod
    def save_regex(
        self,
        path: str,
        *,
        content_digest: str,
        names: Sequence[str],
    ) -> None:
        ...

    @abstractmethod
    def load_validated(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[Dict[str, ModuleRecord]]:
        ...

    @abstractmethod
    def save_validated(
        self,
        path: str,
        modules: Mapping[str, ModuleRecord],
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        ...

    @abstractmethod
    def load_preprocessed(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[str]:
        ...

    @abstractmethod
    def save_preprocessed(
        self,
        path: str,
        text: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        ...

    def cache_artifact_count(self) -> int:
        return 0


class PickleCacheStore(PathWalkCacheStore):
    def __init__(self, cache_root: Path) -> None:
        self._root = Path(cache_root)

    def load_regex(self, path: str, *, content_digest: str) -> Optional[FileRegexCacheEntry]:
        sidecar = _pickle_regex_sidecar_path(self._root, path)
        obj = _load_pickle(sidecar, FileRegexCacheEntry)
        if obj is None:
            return None
        if not content_digest or content_digest != obj.content_digest:
            return None
        return obj

    def save_regex(
        self,
        path: str,
        *,
        content_digest: str,
        names: Sequence[str],
    ) -> None:
        if not content_digest:
            return
        entry = FileRegexCacheEntry(content_digest, tuple(names))
        _atomic_pickle_write(_pickle_regex_sidecar_path(self._root, path), entry)

    def load_validated(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[Dict[str, ModuleRecord]]:
        sidecar = _pickle_validated_sidecar_path(
            self._root,
            path,
            defines_digest=defines_digest,
            preprocess_tag=preprocess_tag,
        )
        obj = _load_pickle(sidecar, FileValidatedCacheEntry)
        if obj is None:
            return None
        if not content_digest or content_digest != obj.content_digest:
            return None
        if obj.defines_digest != defines_digest:
            return None
        if include_closure_digest and obj.include_closure_digest != include_closure_digest:
            return None
        stored_tag = obj.preprocess_tag or "inc"
        if preprocess_tag and stored_tag != preprocess_tag:
            return None
        return {name: rec for name, rec in obj.modules}

    def save_validated(
        self,
        path: str,
        modules: Mapping[str, ModuleRecord],
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        if not content_digest:
            return
        entry = FileValidatedCacheEntry(
            content_digest,
            defines_digest,
            tuple((n, r) for n, r in sorted(modules.items())),
            include_closure_digest,
            preprocess_tag,
        )
        _atomic_pickle_write(
            _pickle_validated_sidecar_path(
                self._root,
                path,
                defines_digest=defines_digest,
                preprocess_tag=preprocess_tag,
            ),
            entry,
        )

    def load_preprocessed(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[str]:
        sidecar = _pickle_preprocessed_sidecar_path(
            self._root,
            path,
            defines_digest=defines_digest,
            include_closure_digest=include_closure_digest,
            preprocess_tag=preprocess_tag,
        )
        obj = _load_pickle(sidecar, FilePreprocessedCacheEntry)
        if obj is None:
            return None
        if not content_digest or content_digest != obj.content_digest:
            return None
        if obj.defines_digest != defines_digest:
            return None
        if obj.include_closure_digest != include_closure_digest:
            return None
        stored_tag = obj.preprocess_tag or "inc"
        if preprocess_tag and stored_tag != preprocess_tag:
            return None
        return obj.text

    def save_preprocessed(
        self,
        path: str,
        text: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        if not content_digest:
            return
        entry = FilePreprocessedCacheEntry(
            content_digest,
            defines_digest,
            include_closure_digest,
            text,
            preprocess_tag,
        )
        _atomic_pickle_write(
            _pickle_preprocessed_sidecar_path(
                self._root,
                path,
                defines_digest=defines_digest,
                include_closure_digest=include_closure_digest,
                preprocess_tag=preprocess_tag,
            ),
            entry,
        )

    def cache_artifact_count(self) -> int:
        count = 0
        for sub in ("regex", "validated", "preprocessed"):
            d = self._root / sub
            if d.is_dir():
                count += sum(1 for p in d.iterdir() if p.suffix == ".pkl")
        return count


class SqliteCacheStore(PathWalkCacheStore):
    def __init__(self, cache_root: Path) -> None:
        self._root = Path(cache_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._db_path = self._root / "pw_cache.sqlite"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pw_cache_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pw_cache_regex (
                    file_token TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    module_names BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pw_cache_validated (
                    file_token TEXT NOT NULL,
                    defines_digest TEXT NOT NULL,
                    preprocess_tag TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    include_closure_digest TEXT NOT NULL,
                    modules BLOB NOT NULL,
                    PRIMARY KEY (file_token, defines_digest, preprocess_tag)
                );
                CREATE TABLE IF NOT EXISTS pw_cache_preprocessed (
                    file_token TEXT NOT NULL,
                    defines_digest TEXT NOT NULL,
                    include_closure_digest TEXT NOT NULL,
                    preprocess_tag TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    text BLOB NOT NULL,
                    PRIMARY KEY (
                        file_token,
                        defines_digest,
                        include_closure_digest,
                        preprocess_tag
                    )
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO pw_cache_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(PW_CACHE_SCHEMA_VERSION)),
            )
            self._conn.commit()

    def _resolved(self, path: str) -> str:
        return str(Path(path).resolve())

    def load_regex(self, path: str, *, content_digest: str) -> Optional[FileRegexCacheEntry]:
        token = file_cache_token(path)
        row = self._conn.execute(
            "SELECT content_digest, module_names FROM pw_cache_regex WHERE file_token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        digest, blob = row
        if not content_digest or content_digest != digest:
            return None
        try:
            names = pickle.loads(blob)
        except (pickle.PickleError, EOFError, ValueError, TypeError):
            return None
        if not isinstance(names, tuple):
            return None
        return FileRegexCacheEntry(digest, names)

    def save_regex(
        self,
        path: str,
        *,
        content_digest: str,
        names: Sequence[str],
    ) -> None:
        if not content_digest:
            return
        token = file_cache_token(path)
        blob = pickle.dumps(tuple(names), protocol=pickle.HIGHEST_PROTOCOL)
        resolved = self._resolved(path)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pw_cache_regex(file_token, path, content_digest, module_names)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(file_token) DO UPDATE SET
                    path = excluded.path,
                    content_digest = excluded.content_digest,
                    module_names = excluded.module_names
                """,
                (token, resolved, content_digest, blob),
            )
            self._conn.commit()

    def load_validated(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[Dict[str, ModuleRecord]]:
        token = file_cache_token(path)
        tag = preprocess_tag or "inc"
        row = self._conn.execute(
            """
            SELECT content_digest, include_closure_digest, preprocess_tag, modules
            FROM pw_cache_validated
            WHERE file_token = ? AND defines_digest = ? AND preprocess_tag = ?
            """,
            (token, defines_digest, tag),
        ).fetchone()
        if row is None:
            return None
        digest, closure, stored_tag, blob = row
        if not content_digest or content_digest != digest:
            return None
        if include_closure_digest and closure != include_closure_digest:
            return None
        if preprocess_tag and stored_tag != preprocess_tag:
            return None
        try:
            modules = pickle.loads(blob)
        except (pickle.PickleError, EOFError, ValueError, TypeError):
            return None
        if not isinstance(modules, tuple):
            return None
        return {name: rec for name, rec in modules}

    def save_validated(
        self,
        path: str,
        modules: Mapping[str, ModuleRecord],
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        if not content_digest:
            return
        token = file_cache_token(path)
        tag = preprocess_tag or "inc"
        blob = pickle.dumps(
            tuple((n, r) for n, r in sorted(modules.items())),
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        resolved = self._resolved(path)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pw_cache_validated(
                    file_token, defines_digest, preprocess_tag, path,
                    content_digest, include_closure_digest, modules
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_token, defines_digest, preprocess_tag) DO UPDATE SET
                    path = excluded.path,
                    content_digest = excluded.content_digest,
                    include_closure_digest = excluded.include_closure_digest,
                    modules = excluded.modules
                """,
                (
                    token,
                    defines_digest,
                    tag,
                    resolved,
                    content_digest,
                    include_closure_digest,
                    blob,
                ),
            )
            self._conn.commit()

    def load_preprocessed(
        self,
        path: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> Optional[str]:
        token = file_cache_token(path)
        tag = preprocess_tag or "inc"
        row = self._conn.execute(
            """
            SELECT content_digest, text
            FROM pw_cache_preprocessed
            WHERE file_token = ? AND defines_digest = ?
              AND include_closure_digest = ? AND preprocess_tag = ?
            """,
            (token, defines_digest, include_closure_digest, tag),
        ).fetchone()
        if row is None:
            return None
        digest, text_blob = row
        if not content_digest or content_digest != digest:
            return None
        if isinstance(text_blob, memoryview):
            text_blob = text_blob.tobytes()
        if isinstance(text_blob, bytes):
            return text_blob.decode("utf-8")
        return str(text_blob)

    def save_preprocessed(
        self,
        path: str,
        text: str,
        *,
        content_digest: str,
        defines_digest: str,
        include_closure_digest: str,
        preprocess_tag: str,
    ) -> None:
        if not content_digest:
            return
        token = file_cache_token(path)
        tag = preprocess_tag or "inc"
        resolved = self._resolved(path)
        payload = text.encode("utf-8")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pw_cache_preprocessed(
                    file_token, defines_digest, include_closure_digest, preprocess_tag,
                    path, content_digest, text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    file_token, defines_digest, include_closure_digest, preprocess_tag
                ) DO UPDATE SET
                    path = excluded.path,
                    content_digest = excluded.content_digest,
                    text = excluded.text
                """,
                (
                    token,
                    defines_digest,
                    include_closure_digest,
                    tag,
                    resolved,
                    content_digest,
                    payload,
                ),
            )
            self._conn.commit()

    def cache_artifact_count(self) -> int:
        total = 0
        for table in (
            "pw_cache_regex",
            "pw_cache_validated",
            "pw_cache_preprocessed",
        ):
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row:
                total += int(row[0])
        return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_cache_store(cache_root: Path, backend: str) -> PathWalkCacheStore:
    token = (backend or "pickle").strip().lower()
    if token in ("sqlite", "sql"):
        return SqliteCacheStore(cache_root)
    return PickleCacheStore(cache_root)


def tier0_load_regex_worker(
    cache_root: str,
    path: str,
    content_digest: str,
    *,
    backend: str = "",
) -> Optional[Tuple[str, ...]]:
    if not cache_root:
        return None
    from hierwalk.perf import pw_cache_backend

    store = open_cache_store(Path(cache_root), backend or pw_cache_backend())
    hit = store.load_regex(path, content_digest=content_digest)
    if hit is None:
        return None
    return tuple(hit.module_names)


def tier0_save_regex_worker(
    cache_root: str,
    path: str,
    content_digest: str,
    names: Sequence[str],
    *,
    backend: str = "",
) -> None:
    if not cache_root or not content_digest:
        return
    from hierwalk.perf import pw_cache_backend

    store = open_cache_store(Path(cache_root), backend or pw_cache_backend())
    store.save_regex(path, content_digest=content_digest, names=names)