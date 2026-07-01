"""Test subprocess env: PYTHONPATH from this file's checkout location."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import bootstrap_path

SRC_ROOT = bootstrap_path.src_root_from(__file__)


def pythonpath(*, merge: bool = True) -> str:
    return bootstrap_path.pythonpath_for(SRC_ROOT, merge=merge)


def subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    merge_pythonpath: bool = True,
) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath(merge=merge_pythonpath)
    if extra:
        env.update(extra)
    return env