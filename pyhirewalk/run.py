#!/usr/bin/env python3
"""
Run a company-style JSON config:

  python3 run.py path/to/run.json
  python3 run.py path/to/run.json --define FOO=1 --json

No pip install and no ``python -m`` required.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyhirewalk.cli import main


def _argv_for_run(argv: list[str]) -> list[str]:
    if argv and argv[0] in ("run", "build-db", "filelist"):
        return argv
    return ["run", *argv]


if __name__ == "__main__":
    raise SystemExit(main(_argv_for_run(sys.argv[1:])))
