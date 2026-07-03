#!/usr/bin/env python3
"""Self-check: confident path-walk must not pp-t0 unrelated filelist RTL."""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

# repo src on path when run as script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect

_PATH_STUBS = 7


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
    path_rtls = [tmp / "top.v"]
    for i in range(_PATH_STUBS):
        p = tmp / f"path_stub_{i}.v"
        p.write_text(f"module path_stub_{i}; endmodule\n", encoding="utf-8")
        path_rtls.append(p)
    for i in range(noise_count):
        (tmp / f"noise_{i}.v").write_text(
            f"module noise_{i}; endmodule\n",
            encoding="utf-8",
        )
    path_f = tmp / "path.f"
    path_f.write_text(
        "\n".join(str(p.resolve()) for p in path_rtls) + "\n",
        encoding="utf-8",
    )
    noise_f = tmp / "noise.f"
    noise_f.write_text(
        "\n".join(str((tmp / f"noise_{i}.v").resolve()) for i in range(noise_count))
        + "\n",
        encoding="utf-8",
    )
    mega_f = tmp / "mega.f"
    mega_f.write_text(f"-f {path_f.name}\n-f {noise_f.name}\n", encoding="utf-8")
    return mega_f


def _run_connect(
    fl,
    tmp: Path,
    *,
    jobs: int,
    no_cache: bool,
    cache_dir: Path | None,
) -> tuple[str, object, object, object]:
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
            no_cache=no_cache,
            jobs=jobs,
            cache_dir=cache_dir,
        )
    finally:
        sys.stderr = old_stderr
    return stderr_capture.getvalue(), batch, _index, state


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
    worker_lines = []
    sync_lines = []
    disk_hit_lines = []
    for ln in t0_lines:
        m = re.search(r"\[hier-walk pp\] pp-t0 (\S+)", ln)
        if m:
            names.append(m.group(1))
        if " worker " in ln:
            worker_lines.append(ln)
        if " sync " in ln:
            sync_lines.append(ln)
    for ln in t0_hit_lines:
        if " disk " in ln:
            disk_hit_lines.append(ln)
    thru = re.findall(r"thru=(\d+)/(\d+)", text)
    thru_bad = [t for t in thru if int(t[1]) >= design_sources]
    missing_ms = [ln for ln in all_t0 if "ms" not in ln]
    return {
        "pp_t0": len(t0_lines),
        "pp_t0_hit": len(t0_hit_lines),
        "unique_files": Counter(names),
        "noise_hits": sum(1 for n in names if n.startswith("noise_")),
        "path_hits": sum(1 for n in names if n == "top.v"),
        "thru_samples": thru[:5],
        "thru_bad": thru_bad,
        "worker_lines": worker_lines,
        "sync_lines": sync_lines,
        "disk_hit_lines": disk_hit_lines,
        "scope_samples": t0_lines[:5],
        "all_t0_lines": all_t0,
        "missing_ms": missing_ms,
    }


def main() -> int:
    os.environ["HIERWALK_PP_LOG"] = "1"
    os.environ.pop("HIERWALK_PW_TIER0_GLOBAL", None)

    with tempfile.TemporaryDirectory(prefix="pw-pp-verify-") as td:
        tmp = Path(td)
        mega_f = _write_design(tmp, noise_count=40)
        fl = parse_filelist(str(mega_f), index_cwd=str(tmp))
        cache_dir = tmp / "pw-cache"

        log_cold, batch, _index, state = _run_connect(
            fl, tmp, jobs=4, no_cache=True, cache_dir=cache_dir
        )
        log_warm, batch2, _index2, _state2 = _run_connect(
            fl, tmp, jobs=1, no_cache=False, cache_dir=cache_dir
        )
        log = log_cold + "\n" + log_warm

        sources_n = len(state.mod_db._sources)
        rep = _analyze_pp_log(log, design_sources=sources_n)

        print("=== pp-t0 scope verification ===")
        print(f"sources in mod_db: {sources_n}")
        print(f"connect ok: {batch.results[0].connected}")
        print(f"warm connect ok: {batch2.results[0].connected}")
        print(f"defer_count: {state.mod_db.defer_count()}")
        print(f"recovery_passes: {state.stats.recovery_passes}")
        print(f"pp-t0 lines: {rep['pp_t0']} (hit: {rep['pp_t0_hit']})")
        print(f"pp-t0 worker lines: {len(rep['worker_lines'])}")
        print(f"pp-t0 sync lines: {len(rep['sync_lines'])}")
        print(f"pp-t0-hit disk lines: {len(rep['disk_hit_lines'])}")
        print(f"pp-t0 on top.v: {rep['path_hits']}")
        print(f"pp-t0 on noise_*.v: {rep['noise_hits']}")
        if rep["thru_samples"]:
            print(f"thru samples: {rep['thru_samples']}")
        if rep["thru_bad"]:
            print(f"thru BAD (denom>=design): {rep['thru_bad']}")
        print("top files by pp-t0 count:")
        for name, cnt in rep["unique_files"].most_common(10):
            print(f"  {cnt:4d}  {name}")
        if rep["scope_samples"]:
            print("sample pp-t0 lines (scope tags):")
            for ln in rep["scope_samples"]:
                print(f"  {ln}")
        if rep["disk_hit_lines"]:
            print("sample pp-t0-hit disk:")
            for ln in rep["disk_hit_lines"][:3]:
                print(f"  {ln}")

        has_sync_path = bool(rep["sync_lines"] or rep["disk_hit_lines"])
        ok = (
            batch.results[0].connected
            and batch2.results[0].connected
            and rep["noise_hits"] == 0
            and rep["pp_t0"] < sources_n
            and sources_n == 41 + _PATH_STUBS
            and state.mod_db.defer_count() == 0
            and not rep["missing_ms"]
            and not rep["thru_bad"]
            and len(rep["worker_lines"]) >= 1
            and has_sync_path
        )
        if rep["missing_ms"]:
            print(f"pp-t0 lines missing ms: {rep['missing_ms'][:3]}")
        if not ok:
            print("\nFAIL: scope contract violated")
            if rep["noise_hits"]:
                noise_files = sorted(
                    {n for n in rep["unique_files"] if n.startswith("noise_")}
                )
                print(f"  noise files touched: {noise_files[:10]}")
            if not rep["worker_lines"]:
                print("  no worker pp-t0 lines (parallel path not exercised)")
            if not has_sync_path:
                print("  no sync/disk pp-t0 path (warm cache pass missing)")
            return 1
        print("\nPASS: scoped pp-t0 only (path.f), worker+disk ms present")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())