"""Load ``_path_bootstrap`` from the same directory as the calling entry file."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def bootstrap_from(entry_file: str | Path) -> Path | None:
    pkg_dir = Path(entry_file).resolve().parent
    boot = pkg_dir / "_path_bootstrap.py"
    if not boot.is_file():
        return None
    name = "_hierwalk_path_bootstrap"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, boot)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    result = mod.ensure_src_on_sys_path(entry_file)
    return result