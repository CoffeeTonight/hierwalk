#!/usr/bin/env python3
"""
Essential index DB builder — run as a plain script:

  python3 build_db.py --config run.json
  python3 build_db.py design.f --cwd . --top chip -o out.sqlite
  python3 build_db.py -c run.json --define EXTRA=1 --json

No pip install and no ``python -m`` required.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow ``import pyhirewalk`` when this file is executed directly.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyhirewalk.cli import main


def _argv_for_build_db(argv: list[str]) -> list[str]:
    """Prepend subcommand ``build-db`` so users omit it on the CLI."""
    if argv and argv[0] in ("build-db", "filelist", "run"):
        return argv
    return ["build-db", *argv]


if __name__ == "__main__":
    raise SystemExit(main(_argv_for_build_db(sys.argv[1:])))
