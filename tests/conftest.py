"""Shared pytest hooks for hierwalk."""

from __future__ import annotations

import pytest

from hierwalk.path_walk import clear_path_walk_suite_session


@pytest.fixture(autouse=True)
def _reset_path_walk_suite_session():
    yield
    clear_path_walk_suite_session()