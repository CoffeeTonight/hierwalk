#!/usr/bin/env python3
"""Self-check: confident path-walk must not pp-t0 unrelated or co-listed RTL."""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect

_ALLOWED_T0 = frozenset({"top.v"})


def _write_design(tmp: Path, *, noise_count: int = 40) -> Path:
    (tmp / "top.v").write_text(
        "module leaf(input in); endmodule\n"
        "module mid;\n"
        "  leaf u_leaf (.in(1'b0));\n"
        "endmodule\n"
        "module top;\n"
        "  mid u_mid ();\n"
        "endmodule\n",
        encoding="utf-8",
    )
    for i in range(8):
        p = tmp / f"path_stub_{i}.v"
        p.write_text(f"module path_stub_{i}; endmodule\n", encoding="utf-8")
    for i in range(noise_count):
        (tmp / f"noise_{i}.v").write_text(
            f"module noise_{i}; endmodule\n",
            encoding="utf-8",
        )
    path_f = tmp / "path.f"
    path_f.write_text(f"{(tmp / 'top.v').resolve()}\n", encoding="utf-8")
    noise_f = tmp / "noise.f"
    noise_f.write_text(
        "\n".join(str((tmp / f"noise_{i}.v").resolve()) for i in range(noise_count))
        + "\n",
        encoding="utf-8",
    )
    mega_f = tmp / "mega.f"
    mega_f.write_text(f"-f {path_f.name}\n-f {noise_f.name}\n", encoding="utf-8")
    return mega_f


def _analyze_pp_log(text: str, *, design_sources: int) -> dict:
    t0_lines = [
        ln
        for ln in text.splitlines()
        if "[hier-walk pp] pp-t0" in ln and "pp-t0-hit" not in ln
    ]
    t0_hit_lines = [
        ln for ln in text.splitlines() if "[hier-walk pp] pp-t0-hit" in ln
    ]
    all_t0 = t0_lines + t0_hit_lines
    names = []
    for ln in t0_lines:
        m = re.search(r"\[hier-walk pp\] pp-t0 (\S+)", ln)
        if m:
            names.append(m.group(1))
    thru = re.findall(r"thru=(\d+)/(\d+)", text)
    thru_bad = [t for t in thru if int(t[1]) >= design_sources]
    extra = sorted({n for n in names if n not in _ALLOWED_T0})
    return {
        "pp_t0": len(t0_lines),
        "unique_files": Counter(names),
        "noise_hits": sum(1 for n in names if n.startswith("noise_")),
        "stub_hits": sum(1 for n in names if n.startswith("path_stub_")),
        "extra_files": extra,
        "thru_samples": thru[:5],
        "thru_bad": thru_bad,
        "all_t0_lines": all_t0,
        "missing_ms": [ln for ln in all_t0 if "ms" not in ln],
    }


def main() -> int:
    os.environ["HIERWALK_PP_LOG"] = "1"
    os.environ.pop("HIERWALK_PW_TIER0_GLOBAL", None)

    with tempfile.TemporaryDirectory(prefix="pw-pp-verify-") as td:
        tmp = Path(td)
        mega_f = _write_design(tmp, noise_count=40)
        fl = parse_filelist(str(mega_f), index_cwd=str(tmp))
        req = ConnectivityRequest(
            checks=(
                ConnectivityCheck("top.u_mid.u_leaf.in", "top.u_mid.u_leaf.in"),
            ),
            top="top",
        )
        stderr_capture = io.StringIO()
        old_stderr = sys.stderr

        class Tee:
            def write(self, s: str) -> int:
                stderr_capture.write(s)
                return old_stderr.write(s)

            def flush(self) -> None:
                old_stderr.flush()

        sys.stderr = Tee()  # type: ignore[assignment]
        try:
            batch, _index, state = run_path_walk_connect(
                req,
                fl,
                top="top",
                no_cache=True,
                jobs=4,
            )
        finally:
            sys.stderr = old_stderr

        log = stderr_capture.getvalue()
        sources_n = len(state.mod_db._sources)
        rep = _analyze_pp_log(log, design_sources=sources_n)

        print("=== pp-t0 scope verification ===")
        print(f"sources in mod_db: {sources_n}")
        print(f"connect ok: {batch.results[0].connected}")
        print(f"defer_count: {state.mod_db.defer_count()}")
        print(f"pp-t0 lines: {rep['pp_t0']}")
        print(f"pp-t0 on top.v: {rep['unique_files'].get('top.v', 0)}")
        print(f"pp-t0 on noise_*.v: {rep['noise_hits']}")
        print(f"pp-t0 on path_stub_*.v: {rep['stub_hits']}")
        if rep["thru_samples"]:
            print(f"thru samples: {rep['thru_samples']}")
        if rep["extra_files"]:
            print(f"extra pp-t0 files: {rep['extra_files']}")
        for ln in rep["all_t0_lines"]:
            print(f"  {ln}")

        ok = (
            batch.results[0].connected
            and rep["noise_hits"] == 0
            and rep["stub_hits"] == 0
            and not rep["extra_files"]
            and rep["pp_t0"] == 1
            and state.mod_db.defer_count() == 0
            and not rep["missing_ms"]
            and not rep["thru_bad"]
        )
        if not ok:
            print("\nFAIL: only top.v may be tier0-scanned on this connect path")
            return 1
        print("\nPASS: pp-t0 only top.v (walked path RTL)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())