"""Startup banner."""

from hierwalk import __version__
from hierwalk.startup import startup_banner_lines


def test_startup_banner_mentions_suite_and_modes():
    lines = startup_banner_lines(version=__version__, pkg_dir="/pkg/hierwalk")
    assert len(lines) == 2
    assert __version__ in lines[0]
    assert "run_on_full_index" in lines[1]
    assert "path-walk" in lines[1]
    assert "--help-config" in lines[1]