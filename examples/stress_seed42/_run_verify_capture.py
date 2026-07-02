#!/usr/bin/env python3
"""One-shot path-walk + pp-log capture for tier0 restore verification."""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_SRC = ROOT.parent.parent / "src"
OUT = ROOT / "_verify_capture_report.txt"


def run(cmd: list[str], *, env: dict[str, str], cwd: Path) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    return proc.returncode, proc.stdout, time.perf_counter() - t0


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_SRC)
    env["HIERWALK_PP_LOG"] = "1"
    lines: list[str] = []

    # pytest: tier0 must not call preprocess
    code, out, sec = run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_SRC.parent / "tests" / "test_connect_pipeline_fixes.py::test_tier1_reuses_preprocessed_text_cache"),
            "-q",
            "--tb=short",
        ],
        env=env,
        cwd=ROOT,
    )
    lines.append(f"=== pytest test_tier1_reuses_preprocessed_text_cache exit={code} {sec:.2f}s ===")
    lines.append(out.strip())

    # path-walk example (text-conn only, 1 RTL file)
    code2, out2, sec2 = run(
        [sys.executable, "-m", "hierwalk", "path_walk_example.json"],
        env=env,
        cwd=ROOT,
    )
    lines.append(f"\n=== hierwalk path_walk_example.json exit={code2} {sec2:.2f}s ===")
    lines.append(out2.strip())

    combined = out2
    counts = {
        "pw-db init": len(re.findall(r"pw-db init", combined)),
        "pp-miss": len(re.findall(r"\[hier-walk pp\] pp-miss", combined)),
        "pp-miss tier0": len(re.findall(r"pp-miss .* tier0", combined)),
        "pp-t0": len(re.findall(r"\[hier-walk pp\] pp-t0\b", combined)),
        "pp-t0-hit": len(re.findall(r"\[hier-walk pp\] pp-t0-hit", combined)),
        "pp-t1": len(re.findall(r"\[hier-walk pp\] pp-t1\b", combined)),
        "pp-defines": len(re.findall(r"\[hier-walk pp\] pp-defines", combined)),
    }
    lines.append("\n=== counts (stderr) ===")
    for k, v in counts.items():
        lines.append(f"  {k}: {v}")

    log_path = ROOT / ".db_stress_top" / "path_walk_example_conn.hier-walk.log"
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(log_text.splitlines()[-40:])
        log_counts = {
            "pp-miss in log": len(re.findall(r"pp-miss", log_text)),
            "pw-db init in log": len(re.findall(r"pw-db init", log_text)),
            "pp-t0 in log": len(re.findall(r"pp-t0", log_text)),
        }
        lines.append(f"\n=== log file {log_path} ===")
        for k, v in log_counts.items():
            lines.append(f"  {k}: {v}")
        lines.append("--- tail ---")
        lines.append(tail)
    else:
        lines.append(f"\n(no log at {log_path})")

    report = "\n".join(lines) + "\n"
    OUT.write_text(report, encoding="utf-8")
    print(report)
    return 0 if code == 0 and code2 == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())