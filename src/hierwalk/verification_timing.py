"""Verification step and per-item timing for run logs."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, TextIO, Tuple

from hierwalk.progress import format_duration

_PREFIX = "[hier-walk verify-timing]"


@dataclass(frozen=True)
class ItemTiming:
    label: str
    elapsed_sec: float
    endpoint_a: str = ""
    endpoint_b: str = ""


@dataclass
class StepTiming:
    kind: str
    name: str
    elapsed_sec: float = 0.0
    items: List[ItemTiming] = field(default_factory=list)


@dataclass
class VerificationTimingRecorder:
    """Accumulates verification step and item timings for one suite run."""

    steps: List[StepTiming] = field(default_factory=list)
    log_paths: List[Path] = field(default_factory=list)
    quiet: bool = False
    _active: Optional[StepTiming] = None
    _t0: float = 0.0

    def register_log_path(self, path: Optional[Path]) -> None:
        if path is None:
            return
        resolved = Path(path)
        if resolved not in self.log_paths:
            self.log_paths.append(resolved)

    def begin_step(self, kind: str, name: str) -> None:
        self._active = StepTiming(kind=kind, name=name)
        self._t0 = time.perf_counter()

    def record_item(
        self,
        label: str,
        elapsed_sec: float,
        *,
        endpoint_a: str = "",
        endpoint_b: str = "",
    ) -> None:
        item = ItemTiming(
            label=label,
            elapsed_sec=elapsed_sec,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
        )
        if self._active is not None:
            self._active.items.append(item)
        self._emit_item_done(item)

    def end_step(self) -> Optional[StepTiming]:
        if self._active is None:
            return None
        self._active.elapsed_sec = time.perf_counter() - self._t0
        step = self._active
        self.steps.append(step)
        self._active = None
        self._emit_step_done(step)
        return step

    @property
    def total_sec(self) -> float:
        return sum(step.elapsed_sec for step in self.steps)

    def _streams(self) -> List[TextIO]:
        return [] if self.quiet else [sys.stderr]

    def _write(self, line: str) -> None:
        for stream in self._streams():
            print(line, file=stream, flush=True)
        for path in self.log_paths:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    print(line, file=fh, flush=True)
            except OSError:
                pass

    def _format_item_detail(self, item: ItemTiming) -> str:
        if item.endpoint_a and item.endpoint_b:
            detail = f"{item.endpoint_a} -> {item.endpoint_b}"
            if item.label and item.label not in detail:
                detail = f"id={item.label} {detail}"
            return detail
        return item.label

    def _emit_item_done(self, item: ItemTiming) -> None:
        detail = self._format_item_detail(item)
        self._write(f"{_PREFIX}   item {detail} {format_duration(item.elapsed_sec)}")

    def _emit_step_done(self, step: StepTiming) -> None:
        n = len(step.items)
        word = "item" if n == 1 else "items"
        self._write(
            f"{_PREFIX} step {step.name} kind={step.kind} "
            f"done {format_duration(step.elapsed_sec)} ({n} {word})"
        )

    def emit_summary(self) -> None:
        if not self.steps:
            return
        lines = [
            f"{_PREFIX} summary total {format_duration(self.total_sec)} "
            f"across {len(self.steps)} step(s)",
        ]
        for step in self.steps:
            n = len(step.items)
            word = "item" if n == 1 else "items"
            lines.append(
                f"{_PREFIX}   {step.name} kind={step.kind} "
                f"{format_duration(step.elapsed_sec)} ({n} {word})"
            )
            for item in step.items:
                lines.append(
                    f"{_PREFIX}     item {self._format_item_detail(item)} "
                    f"{format_duration(item.elapsed_sec)}"
                )
        for line in lines:
            self._write(line)


_suite_recorder: Optional[VerificationTimingRecorder] = None
_active_recorder: Optional[VerificationTimingRecorder] = None


def bind_suite_recorder(recorder: Optional[VerificationTimingRecorder]) -> None:
    global _suite_recorder
    _suite_recorder = recorder


def suite_recorder() -> VerificationTimingRecorder:
    global _suite_recorder
    if _suite_recorder is None:
        _suite_recorder = VerificationTimingRecorder()
    return _suite_recorder


def get_active_recorder() -> Optional[VerificationTimingRecorder]:
    return _active_recorder


def set_active_recorder(recorder: Optional[VerificationTimingRecorder]) -> None:
    global _active_recorder
    _active_recorder = recorder


def record_connect_check(
    *,
    check_id: str,
    endpoint_a: str,
    endpoint_b: str,
    elapsed_sec: float,
) -> None:
    rec = get_active_recorder()
    if rec is None:
        return
    label = check_id or f"{endpoint_a} -> {endpoint_b}"
    rec.record_item(
        label,
        elapsed_sec,
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
    )


def record_verification_item(label: str, elapsed_sec: float) -> None:
    rec = get_active_recorder()
    if rec is None:
        return
    rec.record_item(label, elapsed_sec)


@contextmanager
def verification_step(
    *,
    kind: str,
    name: str,
    recorder: VerificationTimingRecorder,
    log_path: Optional[Path] = None,
) -> Iterator[None]:
    recorder.register_log_path(log_path)
    recorder.begin_step(kind, name)
    prev = _active_recorder
    set_active_recorder(recorder)
    try:
        yield
    finally:
        recorder.end_step()
        set_active_recorder(prev)


def verification_kind_for_mode(mode: str) -> Optional[str]:
    from hierwalk.run_request import RUN_CONN_CHECK, RUN_CONE_TRACE, RUN_IO_TRACE

    key = (mode or "").strip().lower().replace("_", "-")
    if key in ("check-connect", "check-connect-batch", "path-walk"):
        return RUN_CONN_CHECK
    if key == "inst-trace":
        return RUN_IO_TRACE
    if key in ("cone", "fanin-cone", "fanout-cone"):
        return RUN_CONE_TRACE
    return None


def verification_step_label(cfg) -> Optional[Tuple[str, str]]:
    """Return (kind, name) when *cfg* schedules a verification run."""
    from hierwalk.run_request import RUN_CONN_CHECK, RUN_CONE_TRACE, RUN_IO_TRACE
    from hierwalk.run_request import normalize_run_mode

    if getattr(cfg, "verification_step_kind", ""):
        kind = str(cfg.verification_step_kind)
        name = str(getattr(cfg, "verification_step_name", "") or kind)
        return kind, name

    mode = normalize_run_mode(cfg.mode or "")
    kind = verification_kind_for_mode(mode)
    if kind is None:
        if cfg.inst_trace is not None:
            kind = RUN_IO_TRACE
        elif cfg.fanin_cone or cfg.fanout_cone:
            kind = RUN_CONE_TRACE
        elif cfg.check_connect or cfg.check_connect_batch or cfg.connect_inline:
            kind = RUN_CONN_CHECK
        else:
            return None
    name = kind
    if cfg.inst_trace is not None:
        name = cfg.inst_trace.instance or name
    elif cfg.fanout_cone:
        name = cfg.fanout_cone
    elif cfg.fanin_cone:
        name = cfg.fanin_cone
    return kind, name


def is_verification_run_config(cfg) -> bool:
    return verification_step_label(cfg) is not None