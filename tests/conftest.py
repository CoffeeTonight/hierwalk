"""Shared pytest hooks for hierwalk."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import bootstrap_path

bootstrap_path.ensure_src_on_sys_path(__file__)
os.environ["PYTHONPATH"] = bootstrap_path.pythonpath_for(
    bootstrap_path.src_root_from(__file__)
)

import pytest

from hierwalk.manifest import clear_digest_scope
from hierwalk.path_walk import clear_path_walk_suite_session


@pytest.fixture(autouse=True)
def _reset_path_walk_global_state():
    yield
    clear_path_walk_suite_session()
    clear_digest_scope()