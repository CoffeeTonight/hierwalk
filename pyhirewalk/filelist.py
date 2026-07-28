#!/usr/bin/env python3
"""
Expand an EDA filelist (or run JSON):

  python3 filelist.py design.f --cwd . --top chip
  python3 filelist.py --config run.json --json

No pip install and no ``python -m`` required.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyhirewalk.cli import main


def _argv_for_filelist(argv: list[str]) -> list[str]:
    if argv and argv[0] in ("filelist", "build-db", "run"):
        return argv
    return ["filelist", *argv]


if __name__ == "__main__":
    raise SystemExit(main(_argv_for_filelist(sys.argv[1:])))
