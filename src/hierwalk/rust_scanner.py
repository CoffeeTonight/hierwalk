"""Optional Rust RTL structural scanner (hw-scan binary)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class RustAssignLhs:
    lhs: str


@dataclass(frozen=True)
class RustModuleScan:
    name: str
    kind: str = "module"
    ports: Sequence[str] = ()
    wires: Sequence[str] = ()
    regs: Sequence[str] = ()
    assigns: Sequence[RustAssignLhs] = ()


@dataclass(frozen=True)
class RustFileScan:
    modules: Sequence[RustModuleScan] = ()


def rust_scanner_enabled() -> bool:
    raw = os.environ.get("HIERWALK_RUST_SCANNER", "").strip().lower()
    return raw in ("1", "true", "yes", "on", "rust")


@lru_cache(maxsize=1)
def _hw_scan_binary() -> Optional[Path]:
    env = os.environ.get("HIERWALK_HW_SCAN_BIN", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "rust" / "hw-scan" / "target" / "release" / "hw-scan",
        here.parents[2] / "rust" / "hw-scan" / "target" / "debug" / "hw-scan",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def hw_scan_available() -> bool:
    return _hw_scan_binary() is not None


def scan_file_rust(path: str | Path) -> Optional[RustFileScan]:
    """Run hw-scan on *path*; return None if binary missing or scan fails."""
    binary = _hw_scan_binary()
    if binary is None:
        return None
    resolved = str(Path(path).resolve())
    try:
        proc = subprocess.run(
            [str(binary), resolved],
            check=False,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    modules: List[RustModuleScan] = []
    for raw in data.get("modules") or []:
        if not isinstance(raw, dict):
            continue
        assigns = tuple(
            RustAssignLhs(lhs=str(a.get("lhs", "")))
            for a in (raw.get("assigns") or [])
            if isinstance(a, dict) and a.get("lhs")
        )
        modules.append(
            RustModuleScan(
                name=str(raw.get("name", "")),
                kind=str(raw.get("kind", "module")),
                ports=tuple(str(p) for p in (raw.get("ports") or [])),
                wires=tuple(str(p) for p in (raw.get("wires") or [])),
                regs=tuple(str(p) for p in (raw.get("regs") or [])),
                assigns=assigns,
            )
        )
    return RustFileScan(modules=tuple(modules))


def module_names_from_scan(scan: RustFileScan) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for mod in scan.modules:
        if mod.name and mod.name not in seen:
            seen.add(mod.name)
            names.append(mod.name)
    return names