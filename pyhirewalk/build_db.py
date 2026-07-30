#!/usr/bin/env python3
"""
Essential index builder (class :class:`pyhirewalk.index.BuildDb`).

  python3 build_db.py --config run.json
  python3 build_db.py design.f --cwd . --top chip -o out.sqlite

Also:

  from pyhirewalk.index import BuildDb
  BuildDb("design.f", "out.sqlite", top="chip", mode="fast").run()
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pyhirewalk.cli import main  # noqa: E402
from pyhirewalk.index.build_db import BuildDb, BuildDbResult  # noqa: E402

__all__ = ["BuildDb", "BuildDbResult", "main"]


def _argv_for_build_db(argv: list[str]) -> list[str]:
    if argv and argv[0] in ("build-db", "filelist", "run"):
        return argv
    return ["build-db", *argv]


if __name__ == "__main__":
    raise SystemExit(main(_argv_for_build_db(sys.argv[1:])))
