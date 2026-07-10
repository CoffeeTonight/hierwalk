"""Timestamped logs for hgpath / hgconn (always date+time)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import IO, Optional, TextIO

_PREFIX = "[hg]"


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def emit_hg_log(
    message: str,
    *,
    tool: str = "hg",
    stream: Optional[TextIO] = None,
    log_file: Optional[IO[str]] = None,
) -> None:
    if not message:
        return
    line = f"{_stamp()} [{tool}] {message}"
    out = stream if stream is not None else sys.stderr
    print(line, file=out, flush=True)
    if log_file is not None:
        print(line, file=log_file, flush=True)


def hg_log_path(work_dir: Path, tool: str) -> Path:
    return work_dir / f"{tool}.log"