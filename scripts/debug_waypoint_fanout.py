#!/usr/bin/env python3
"""
PyCharm-friendly waypoint-fanout debugger.

Open project: hierwalk/  (mark ``src`` as Sources Root if imports fail)

Run configurations (``.run/``):
  - debug waypoint fanout          → this script, breakpoint on demand
  - hier-walk waypoint example     → CLI flat JSON
  - pytest waypoint fanout trace   → single test with debugger

Quick start (terminal)::

  cd examples/waypoint_fanout_verify
  python ../../scripts/debug_waypoint_fanout.py
  python ../../scripts/debug_waypoint_fanout.py --break
  python ../../scripts/debug_waypoint_fanout.py --json connect_waypoint.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DEFAULT_EXAMPLE = _ROOT / "examples" / "waypoint_fanout_verify"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Debug waypoint-fanout trace in PyCharm")
    ap.add_argument(
        "--example-dir",
        type=Path,
        default=_DEFAULT_EXAMPLE,
        help=f"bundled RTL + JSON (default: {_DEFAULT_EXAMPLE})",
    )
    ap.add_argument(
        "--json",
        type=Path,
        default=None,
        help="connect batch JSON (default: <example-dir>/connect_waypoint.json)",
    )
    ap.add_argument(
        "--check-id",
        default="wp_dual_fanout",
        help="which check id to run from JSON (default: wp_dual_fanout)",
    )
    ap.add_argument(
        "--direction",
        choices=("fanout", "both"),
        default=None,
        help="override map.direction (fanout | both)",
    )
    ap.add_argument(
        "--a",
        action="append",
        dest="origins",
        help="override fanout origins (repeatable); skips --json check selection",
    )
    ap.add_argument(
        "--b",
        action="append",
        dest="waypoints",
        help="override waypoints (repeatable)",
    )
    ap.add_argument(
        "--path-kind",
        choices=("comb", "ff"),
        default=None,
        help="override map.path_kind",
    )
    ap.add_argument(
        "--break",
        dest="breakpoint",
        action="store_true",
        help="call breakpoint() before/after trace (PyCharm Debug)",
    )
    ap.add_argument(
        "--no-tsv",
        action="store_true",
        help="skip TSV dump",
    )
    return ap.parse_args(argv)


def load_design(example_dir: Path) -> Tuple[Any, Any, str, Mapping[str, Any]]:
    """Build index + flat rows from example filelist. Breakpoint target #1."""
    from hierwalk.filelist import parse_filelist
    from hierwalk.index import DesignIndex
    from hierwalk.elab import elaborate

    example_dir = example_dir.resolve()
    fl_path = example_dir / "filelist.f"
    if not fl_path.is_file():
        raise FileNotFoundError(f"missing filelist: {fl_path}")

    spec = parse_filelist(str(fl_path), index_cwd=str(example_dir))
    if spec.errors:
        raise ValueError(f"filelist errors: {'; '.join(spec.errors)}")
    top = (spec.top_modules[0] if spec.top_modules else None) or "top"
    sources: dict[str, str] = {}
    for src in spec.source_files:
        p = Path(src)
        try:
            sources[str(p.resolve())] = p.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise OSError(f"cannot read RTL {p}: {exc}") from exc

    index = DesignIndex.build(sources)
    _, rows = elaborate(index, top)
    rows_by_path = {r.full_path: r for r in rows}
    return index, rows, top, rows_by_path


def resolve_check(
    args: argparse.Namespace,
    example_dir: Path,
) -> Tuple[List[str], List[str], Any, str, str]:
    """Return (a_specs, b_specs, path_kind, direction, check_id)."""
    if args.origins and args.waypoints:
        path_kind: Any = args.path_kind or "ff"
        direction = args.direction or "both"
        return list(args.origins), list(args.waypoints), path_kind, direction, "cli"

    json_path = (args.json or example_dir / "connect_waypoint.json").resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    checks = data.get("checks") or []
    if not checks:
        raise ValueError(f"no checks in {json_path}")

    chosen = None
    for chk in checks:
        if str(chk.get("id", "")).strip() == args.check_id:
            chosen = chk
            break
    if chosen is None:
        chosen = checks[0]
        print(f"[debug] check id {args.check_id!r} not found; using {chosen.get('id')!r}")

    def _list_endpoint(raw: Any) -> List[str]:
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        text = str(raw or "").strip()
        return [text] if text else []

    a_specs = _list_endpoint(chosen.get("a"))
    b_specs = _list_endpoint(chosen.get("b"))
    map_spec = chosen.get("map") or {}
    path_kind = args.path_kind or map_spec.get("path_kind") or "comb"
    direction = args.direction or str(map_spec.get("direction") or "fanout")
    check_id = str(chosen.get("id") or "wp")
    return a_specs, b_specs, path_kind, direction, check_id


def run_waypoint_debug(
    *,
    index: Any,
    rows: Sequence[Any],
    top: str,
    a_specs: Sequence[str],
    b_specs: Sequence[str],
    path_kind: Any,
    direction: str,
    check_id: str,
) -> Tuple[Any, List[Any]]:
    """Run trace. Set breakpoint inside ``waypoint_fanout._trace_origin_fanout``."""
    from hierwalk.waypoint_fanout import run_waypoint_fanout_check

    return run_waypoint_fanout_check(
        list(a_specs),
        list(b_specs),
        rows=rows,
        index=index,
        top=top,
        path_kind=path_kind,
        direction=direction,
        check_id=check_id,
        endpoint_a=",".join(a_specs),
        endpoint_b=",".join(b_specs),
    )


def print_report(result: Any, events: Sequence[Any], *, show_tsv: bool) -> None:
    from hierwalk.waypoint_fanout import format_waypoint_fanout_tsv

    print(f"mode={result.mode} connected={result.connected}")
    if result.note:
        print(f"note: {result.note}")
    if result.errors:
        print("errors:")
        for err in result.errors:
            print(f"  - {err}")

    terminators = [e for e in events if e.is_terminator == "Y"]
    qualified = [e for e in terminators if e.waypoint_qualified == "Y"]
    unqualified = [e for e in terminators if e.waypoint_qualified != "Y"]
    print(
        f"events={len(events)} terminators={len(terminators)} "
        f"qualified={len(qualified)} unqualified={len(unqualified)}"
    )

    print("\n--- event table (set breakpoint after run to inspect `events`) ---")
    for ev in events:
        print(
            f"{ev.source}\t{ev.event_kind}\t{ev.scope}:{ev.net}\t"
            f"line={ev.rtl_line}\twp_hit={ev.waypoint_hit}\t"
            f"wp_qual={ev.waypoint_qualified}\tterm={ev.is_terminator}"
        )

    if show_tsv:
        print("\n--- TSV ---")
        print(format_waypoint_fanout_tsv(events), end="")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    example_dir = args.example_dir.resolve()
    print(f"[debug] example_dir={example_dir}")

    index, rows, top, rows_by_path = load_design(example_dir)
    a_specs, b_specs, path_kind, direction, check_id = resolve_check(args, example_dir)
    print(f"[debug] check_id={check_id} path_kind={path_kind} direction={direction}")
    print(f"[debug] a={a_specs}")
    print(f"[debug] b={b_specs}")

    if args.breakpoint:
        # Inspect: index, rows, rows_by_path, a_specs, b_specs
        breakpoint()  # noqa: T100

    result, events = run_waypoint_debug(
        index=index,
        rows=rows,
        top=top,
        a_specs=a_specs,
        b_specs=b_specs,
        path_kind=path_kind,
        direction=direction,
        check_id=check_id,
    )

    if args.breakpoint:
        # Inspect: result, events, result.waypoint_events
        breakpoint()  # noqa: T100

    print_report(result, events, show_tsv=not args.no_tsv)
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())