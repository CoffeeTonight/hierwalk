"""stderr progress reporting for hier-walk."""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, TextIO


def log_timestamp() -> str:
    """Local wall-clock stamp for stderr / run logs."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_hierwalk_log(message: str, *, prefix: str = "[hier-walk]") -> str:
    return f"{log_timestamp()} {prefix} {message}"


def format_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


ProgressFn = Callable[[str], None]


def _rtl_path_keys(rtl_file: str) -> tuple[str, ...]:
    p = Path(str(rtl_file).replace("\\", "/"))
    keys: list[str] = []
    for candidate in (p,):
        try:
            candidate = candidate.resolve()
        except OSError:
            pass
        s = str(candidate).replace("\\", "/")
        if s and s not in keys:
            keys.append(s)
    raw = str(rtl_file).replace("\\", "/")
    if raw and raw not in keys:
        keys.append(raw)
    return tuple(keys)


def resolve_listing_filelist(
    rtl_file: str,
    via_map: Optional[Mapping[str, str]],
) -> str:
    """Return the ``.f`` that listed this RTL (basename), if known."""
    if not via_map:
        return ""
    for key in _rtl_path_keys(rtl_file):
        hit = via_map.get(key)
        if hit:
            p = Path(str(hit))
            return p.name or str(p)
    rtl_norm = _rtl_path_keys(rtl_file)[0] if rtl_file else ""
    for src_key, listing in via_map.items():
        src_norm = _rtl_path_keys(src_key)[0] if src_key else ""
        if src_norm and rtl_norm and src_norm == rtl_norm:
            p = Path(str(listing))
            return p.name or str(p)
    return ""


def format_work_location(
    file_path: str,
    *,
    index: Optional[int] = None,
    total: Optional[int] = None,
    listing_filelist: str = "",
    via_map: Optional[Mapping[str, str]] = None,
) -> str:
    """Short folder + file label for heartbeat / progress detail."""
    p = Path(str(file_path).replace("\\", "/"))
    parts = [x for x in p.parent.parts if x]
    if len(parts) > 2:
        folder = "/".join(parts[-2:])
    elif parts:
        folder = "/".join(parts)
    else:
        folder = "."
    listing = listing_filelist or resolve_listing_filelist(file_path, via_map)
    label = f"folder: {folder} | file: {p.name}"
    if listing:
        label = f"listing: {listing} | {label}"
    if index is not None and total is not None:
        label = f"{label} ({index}/{total})"
    return label


def split_progress_detail(message: str) -> Optional[str]:
    """Return the detail suffix after `` — `` when present."""
    if " — " not in message:
        return None
    return message.split(" — ", 1)[1].strip() or None


class ProgressReporter:
    """Lightweight phase lines on stderr."""

    def __init__(self, *, stream: Optional[TextIO] = None, enabled: bool = True) -> None:
        self._stream = stream or sys.stderr
        self._enabled = enabled
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self._filelist_label = ""
        self._active_listing_label = ""
        self._location = ""
        self._current_file = ""
        self._via_map: Optional[Mapping[str, str]] = None

    def phase(self, message: str) -> None:
        if not self._enabled:
            return
        print(format_hierwalk_log(message), file=self._stream, flush=True)

    def set_filelist(self, filelist_path: str) -> None:
        with self._lock:
            self._filelist_label = Path(filelist_path).name or filelist_path

    def set_location(self, detail: str) -> None:
        with self._lock:
            self._location = detail.strip()

    def absorb_progress(self, message: str) -> None:
        """Update location detail from a progress line suffix."""
        suffix = split_progress_detail(message)
        if suffix is not None:
            self._apply_location_detail(suffix)

    def track_work(
        self,
        file_path: str,
        *,
        index: int,
        total: int,
        via_map: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Update heartbeat location without printing a progress line."""
        if via_map is not None:
            self._via_map = via_map
        self._current_file = file_path
        self._apply_location_detail(
            format_work_location(
                file_path,
                index=index,
                total=total,
                via_map=self._via_map,
            )
        )

    def _apply_location_detail(self, detail: str) -> None:
        detail = detail.strip()
        if not detail:
            return
        listing = _parse_listing_label(detail)
        with self._lock:
            self._location = detail
            if listing:
                self._active_listing_label = listing

    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def get_detail(self) -> str:
        with self._lock:
            parts: list[str] = []
            listing = self._active_listing_label
            if not listing and self._current_file and self._via_map:
                listing = resolve_listing_filelist(self._current_file, self._via_map)
            if not listing:
                listing = self._filelist_label
            if listing:
                parts.append(f"filelist: {listing}")
            loc = _strip_listing_prefix(self._location)
            if loc:
                parts.append(loc)
            return " | ".join(parts)


def _parse_listing_label(detail: str) -> str:
    for segment in detail.split("|"):
        seg = segment.strip()
        if seg.startswith("listing:"):
            return seg.split(":", 1)[1].strip()
    return ""


def _strip_listing_prefix(detail: str) -> str:
    if not detail:
        return ""
    segments = [s.strip() for s in detail.split("|")]
    kept = [s for s in segments if not s.startswith("listing:")]
    return " | ".join(kept)


class ProgressSink:
    """Callable progress sink: ``emit`` logs lines; ``track`` updates heartbeat only."""

    def __init__(self, reporter: ProgressReporter) -> None:
        self._reporter = reporter

    def __call__(self, message: str) -> None:
        self.emit(message)

    def emit(self, message: str) -> None:
        self._reporter.phase(message)
        self._reporter.absorb_progress(message)

    def track(
        self,
        file_path: str,
        *,
        index: int,
        total: int,
        via_map: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._reporter.track_work(
            file_path,
            index=index,
            total=total,
            via_map=via_map,
        )


def maybe_track_work(
    on_progress: Optional[ProgressFn],
    file_path: str,
    *,
    index: int,
    total: int,
    via_map: Optional[Mapping[str, str]] = None,
) -> None:
    if on_progress is None:
        return
    track = getattr(on_progress, "track", None)
    if callable(track):
        track(file_path, index=index, total=total, via_map=via_map)


class ProgressHeartbeat:
    """Emit periodic still-running lines during long synchronous work."""

    def __init__(
        self,
        on_phase: ProgressFn,
        label: str,
        *,
        interval_sec: float = 15.0,
        enabled: bool = True,
        get_detail: Optional[Callable[[], str]] = None,
    ) -> None:
        self._on_phase = on_phase
        self._label = label
        self._interval = interval_sec
        self._enabled = enabled
        self._get_detail = get_detail
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = time.perf_counter()

    def __enter__(self) -> "ProgressHeartbeat":
        if not self._enabled:
            return self
        def _loop() -> None:
            while not self._stop.wait(self._interval):
                elapsed = format_duration(time.perf_counter() - self._t0)
                detail = self._get_detail() if self._get_detail else ""
                if detail:
                    self._on_phase(
                        f"{self._label}… still running ({elapsed}) — {detail}"
                    )
                else:
                    self._on_phase(f"{self._label}… still running ({elapsed})")

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


@contextmanager
def null_progress() -> Iterator[None]:
    yield


def progress_callback(reporter: Optional[ProgressReporter]) -> Optional[ProgressSink]:
    if reporter is None or not reporter._enabled:
        return None
    return ProgressSink(reporter)