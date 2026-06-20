"""Bundled comprehensive search example JSON for ``hier-walk --example``."""

from __future__ import annotations

from pathlib import Path

SEARCH_EXAMPLE_FILENAME = "search_example.json"

SEARCH_EXAMPLE_TEXT = """\
{
  // Comprehensive search example — all pattern kinds and search flags.
  // Runnable with bundled stress corpus (from examples/stress_seed42/):
  //   hier-walk search_example.json
  // Or generate a copy anywhere:
  //   hier-walk --example
  //   hier-walk --example my_search.json
  //   hier-walk --example - > search.json

  "filelist": "filelist.f",
  "top": "stress_top",
  "output": "search_hits.tsv",
  "mode": "search",

  // Structured search: instance / path / hierarchy_path (OR across patterns).
  // Top-level search-path below is merged into hierarchy_path.
  //
  // Path pattern syntax (search.path, dotted --search):
  //   .       next hierarchy segment — fixed depth from root (top first)
  //   ..      one or more intermediate segments (only way to span variable hops)
  //   * ?     globs within ONE node name; * never crosses .
  //   no dots any single path segment may match (e.g. *u_spine*)
  //
  //   a.b.*c                     exactly 3 segments from root
  //   stress_top.u_spine.*       exactly 3 segments; third is any child name
  //   top.E*..*log.*cpu*         E*, then 1+ hops, then *log*, then *cpu* adjacent
  //   top.E*..*log..*cpu*        at least one segment between *log* and *cpu*
  //
  // search.instance: inst_leaf name globs (no dot / .. semantics).
  // search-path / hierarchy_path: same segment rules + optional trailing port.
  "search": {
    "instance": ["u_spine", "*spine*"],
    "path": ["stress_top.u_spine.*", "*u_spine*"],
    "hierarchy_path": ["stress_top.u_*.probe_out", "stress_top.u_*"],
    "case_insensitive": true,
    "search_module": true,
    "search_subtree": true
  },
  "search-path": "stress_top.probe_in"
  // Top-level aliases (apply when omitted inside search object):
  // "search-module": true,
  // "search-subtree": true,
  // "search-case-insensitive": true,

  // --- Legacy flat search (replace structured block above) ---
  // Comma-separated OR; dotted tokens route to path, plain tokens to instance.
  // "search": "u_spine,stress_top.u_spine.*",
  // "search-path": "stress_top.u_*.probe_out,stress_top.probe_in",
  // "search-module": true,
  // "search-subtree": true,
  // "search-case-insensitive": true,

  // --- CLI equivalents ---
  // hier-walk filelist.f --top stress_top \\
  //   --search 'u_spine,stress_top.u_spine.*' \\
  //   --search-path 'stress_top.u_*.probe_out' \\
  //   --search-module --search-subtree --search-case-insensitive \\
  //   -o search_hits.tsv

  // --- Flat test suite: run_on_full_index search step ---
  // "run_on_full_index": {
  //   "enable": 1,
  //   "mode": "search",
  //   "output": "suite_search_hits.tsv",
  //   "search": {
  //     "instance": ["u_spine"],
  //     "path": ["stress_top.u_spine.*"],
  //     "hierarchy_path": ["stress_top.u_*.probe_out"],
  //     "case_insensitive": true,
  //     "search_module": true,
  //     "search_subtree": false
  //   },
  //   "search-path": "stress_top.probe_in"
  // }
}
"""


def search_example_text() -> str:
    """Return the JSONC search example document."""
    return SEARCH_EXAMPLE_TEXT


def write_search_example(path: Path) -> Path:
    """Write the example document to *path*; return resolved path."""
    path = path.expanduser()
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(search_example_text(), encoding="utf-8")
    return path.resolve()