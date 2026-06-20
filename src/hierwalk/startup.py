"""Brief identity lines printed at the start of each hier-walk run."""

from __future__ import annotations

from typing import List


def startup_banner_lines(*, version: str, pkg_dir: str) -> List[str]:
    """One-line tool summary after version (stderr)."""
    return [
        f"run: hier-walk {version} ({pkg_dir})",
        (
            "run: flat suite JSON — run_on_full_index | run_conn_check | "
            "run_io_trace | run_cone_trace; block mode full-index|path-walk; "
            "JSONC // ok; --help-config"
        ),
    ]


def emit_startup_banner(
    *,
    version: str,
    pkg_dir: str,
    stream,
) -> None:
    for line in startup_banner_lines(version=version, pkg_dir=pkg_dir):
        print(line, file=stream, flush=True)