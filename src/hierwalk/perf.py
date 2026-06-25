"""Performance helpers: auto low-memory, job resolution."""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_LOW_MEMORY_AUTO_THRESHOLD = 1500
DEFAULT_INCLUDE_WARM_MAX = 200
DEFAULT_BODY_PARAM_SCAN_MAX = 512 * 1024


def low_memory_auto_threshold() -> int:
    """Source count at which fused index build is enabled (0 = disabled)."""
    raw = os.environ.get("HIERWALK_LOW_MEMORY_AUTO", "").strip()
    if raw.lower() in ("0", "off", "false", "no", "disable", "disabled"):
        return 0
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_LOW_MEMORY_AUTO_THRESHOLD


def effective_low_memory(*, explicit: bool, num_sources: int) -> bool:
    if explicit:
        return True
    threshold = low_memory_auto_threshold()
    return threshold > 0 and num_sources >= threshold


def body_param_scan_max() -> int:
    """
    Max module body size (bytes) for scanning ``parameter``/``localparam`` decls.

    Bodies larger than this use header params only at index time (0 = always scan).
    """
    raw = os.environ.get("HIERWALK_BODY_PARAM_SCAN_MAX", "").strip()
    if raw.lower() in ("0", "off", "false", "no", "disable", "disabled"):
        return 0
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_BODY_PARAM_SCAN_MAX


def include_warm_enabled() -> bool:
    """Include warm is opt-in (``HIERWALK_INCLUDE_WARM=1``)."""
    import os

    raw = os.environ.get("HIERWALK_INCLUDE_WARM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def pw_db_build_mode() -> str:
    """
    When to run full tier-1 path-walk DB build.

    ``HIERWALK_PW_DB_BUILD``:
      off (default) — verify-only lazy tier0/tier1 on touched RTL
      after_verify — full DB build after verification output (suite end or step)
    ``HIERWALK_PW_DB_PREFETCH=1`` is an alias for ``after_verify``.
    """
    raw = os.environ.get("HIERWALK_PW_DB_BUILD", "").strip().lower()
    if raw in ("after_verify", "after-verify", "post_verify", "post-verify"):
        return "after_verify"
    if raw in ("off", "0", "false", "no", "disable", "disabled"):
        return "off"
    legacy = os.environ.get("HIERWALK_PW_DB_PREFETCH", "").strip().lower()
    if legacy in ("1", "true", "yes", "on"):
        return "after_verify"
    return "off"


def pw_db_prefetch_enabled() -> bool:
    """True when a post-verify full DB build is configured."""
    return pw_db_build_mode() == "after_verify"


def pw_db_prefetch_wait_on_exit() -> bool:
    """When prefetch is on, wait for the prefetch thread before returning (default on)."""
    raw = os.environ.get("HIERWALK_PW_DB_PREFETCH_WAIT", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def pw_db_prefetch_max_files() -> int:
    """Cap tier-1 prefetch files per run (0 = no limit)."""
    raw = os.environ.get("HIERWALK_PW_DB_PREFETCH_MAX", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def log_large_module_skips() -> bool:
    """When true, stderr notes modules that skip body parameter collection."""
    raw = os.environ.get("HIERWALK_LOG_LARGE_MODULES", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def path_walk_recovery_pass_cap() -> int:
    """Max recovery drain iterations per path-walk session (``HIERWALK_PW_RECOVERY_CAP``)."""
    raw = os.environ.get("HIERWALK_PW_RECOVERY_CAP", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 32


def pw_module_file_cap() -> int:
    """Max tier-0 module files scanned per path-walk resolve step."""
    raw = os.environ.get("HIERWALK_PW_MODULE_FILE_CAP", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 32


def pw_tier0_global_scan_max() -> int:
    """Max tier-0 files in a recovery global expand."""
    raw = os.environ.get("HIERWALK_PW_TIER0_GLOBAL_MAX", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 128


def pw_inst_resolve_tier1_max(policy: str) -> int:
    """Max tier-1 inst-leaf index files per resolve pass (policy-dependent default)."""
    raw = os.environ.get("HIERWALK_PW_TIER1_MAX", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    if str(policy).strip().lower() == "recovery":
        return 24
    return 12


def slow_file_log_threshold_sec() -> Optional[float]:
    """
    Log per-file preprocess/scan timing when a source exceeds this many seconds.

    ``HIERWALK_LOG_SLOW_FILES=1`` uses 10s; ``=30`` uses 30s; unset/0 disables.
    """
    raw = os.environ.get("HIERWALK_LOG_SLOW_FILES", "").strip().lower()
    if raw in ("", "0", "off", "false", "no", "disable", "disabled"):
        return None
    if raw in ("1", "true", "yes", "on"):
        return 10.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 10.0