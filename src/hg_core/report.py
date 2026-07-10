"""Concise phase reports with total elapsed time."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def format_elapsed_sec(start: float, *, end: Optional[float] = None) -> str:
    sec = (end if end is not None else time.perf_counter()) - start
    if sec < 1.0:
        return f"{sec * 1000.0:.1f}ms"
    return f"{sec:.2f}s"


@dataclass
class ReportBuilder:
    """One-phase summary report (hgpath or hgconn)."""

    title: str
    tool: str
    started_at: float = field(default_factory=time.perf_counter)
    lines: List[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        text = str(line).rstrip()
        if text:
            self.lines.append(text)

    def finish(self, path: Path) -> str:
        elapsed = format_elapsed_sec(self.started_at)
        iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = [
            self.title,
            f"  tool: {self.tool}",
            f"  finished_at: {iso}",
            f"  elapsed: {elapsed}",
            "",
            *self.lines,
            "",
        ]
        text = "\n".join(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text