"""Allow: python -m hierwalk [args] (no hier-walk script on PATH required)."""

import importlib.util
from pathlib import Path

_here = Path(__file__).resolve()
_spec = importlib.util.spec_from_file_location(
    "_hierwalk_bootstrap_entry",
    _here.parent / "_bootstrap_entry.py",
)
if _spec is not None and _spec.loader is not None:
    _boot_entry = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_boot_entry)
    _boot_entry.bootstrap_from(_here)

from hierwalk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())