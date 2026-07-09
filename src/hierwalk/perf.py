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


def pw_fl_shell_max() -> int:
    """Max filelist-tree shells (BFS depth) for confident tier-0 decl resolve."""
    raw = os.environ.get("HIERWALK_PW_FL_SHELL_MAX", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 12


def pw_module_file_cap() -> int:
    """Max tier-0 module files scanned per path-walk resolve step."""
    raw = os.environ.get("HIERWALK_PW_MODULE_FILE_CAP", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 32


def pw_tier0_global_enabled() -> bool:
    """Allow tier-0 regex queue to seed from the full design (default off)."""
    raw = os.environ.get("HIERWALK_PW_TIER0_GLOBAL", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


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


def pw_define_follow_includes() -> bool:
    """Path-walk tier1 define accumulate follows `` `include `` (default off)."""
    raw = os.environ.get("HIERWALK_PW_DEFINE_INCLUDES", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def pw_tier1_follow_includes() -> bool:
    """
    Path-walk tier1 preprocess inlines `` `include `` (default off).

    Wrapper RTL that only pulls the design via includes can opt in with
    ``HIERWALK_PW_TIER1_INCLUDES=1`` (may be very slow on large trees).
    """
    raw = os.environ.get("HIERWALK_PW_TIER1_INCLUDES", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def pw_define_accum_max_files() -> int:
    """Cap RTL files per path-walk define-accumulate step (0 = no cap)."""
    raw = os.environ.get("HIERWALK_PW_DEFINE_ACCUM_MAX", "").strip()
    if raw.lower() in ("0", "off", "false", "no", "disable", "disabled"):
        return 0
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 128


def pw_rust_scanner_enabled() -> bool:
    """Use Rust hw-scan for structural RTL scan (``HIERWALK_RUST_SCANNER=1``)."""
    raw = os.environ.get("HIERWALK_RUST_SCANNER", "").strip().lower()
    return raw in ("1", "true", "yes", "on", "rust")


def pw_cache_backend() -> str:
    """
    Path-walk disk cache backend (``HIERWALK_PW_CACHE``).

    ``pickle`` (default) — per-file ``.pkl`` sidecars under regex/validated/preprocessed.
    ``sqlite`` — single ``pw_cache.sqlite`` per cache namespace (stdlib sqlite3).
    """
    raw = os.environ.get("HIERWALK_PW_CACHE", "").strip().lower()
    if raw in ("sqlite", "sql"):
        return "sqlite"
    return "pickle"


def pw_include_closure_direct() -> bool:
    """
    Path-walk tier1 cache keys use direct `` `include `` lines only (default on).

    Full transitive closure is opt-in via ``HIERWALK_PW_INCLUDE_CLOSURE_FULL=1``.
    """
    raw = os.environ.get("HIERWALK_PW_INCLUDE_CLOSURE_FULL", "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def pw_include_closure_max() -> Optional[int]:
    """
    Cap `` `include `` files per path-walk closure digest (0 = no cap).

    Used only when ``pw_include_closure_direct()`` is false.  Default matches
    ``HIERWALK_INCLUDE_WARM_MAX`` (200).
    """
    raw = os.environ.get("HIERWALK_PW_INCLUDE_CLOSURE_MAX", "").strip()
    if not raw:
        warm = os.environ.get("HIERWALK_INCLUDE_WARM_MAX", "").strip()
        if warm.lower() in ("0", "off", "false", "no", "disable", "disabled"):
            return None
        if warm:
            try:
                return max(1, int(warm))
            except ValueError:
                pass
        return DEFAULT_INCLUDE_WARM_MAX
    if raw.lower() in ("0", "off", "false", "no", "disable", "disabled"):
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_INCLUDE_WARM_MAX


def text_grep_prewarm_enabled() -> bool:
    """Opt-in eager text-grep index prewarm (``HIERWALK_TEXT_GREP_PREWARM=1``)."""
    raw = os.environ.get("HIERWALK_TEXT_GREP_PREWARM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def pw_trace_verbose() -> bool:
    """Emit tier0/tier1/resolve/miss pw-db trace lines (``HIERWALK_PW_TRACE_VERBOSE=1``)."""
    raw = os.environ.get("HIERWALK_PW_TRACE_VERBOSE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def pw_heartbeat_interval_sec() -> Optional[float]:
    """
    Periodic heartbeat on stderr/log during long pw-db resolves and connect-coi.

    ``HIERWALK_PW_HEARTBEAT=1`` uses 30s; ``=60`` uses 60s; unset/0 disables.
    """
    raw = os.environ.get("HIERWALK_PW_HEARTBEAT", "").strip().lower()
    if raw in ("", "0", "off", "false", "no", "disable", "disabled"):
        return None
    if raw in ("1", "true", "yes", "on"):
        return 30.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 30.0


def hgrep_heartbeat_interval_sec() -> Optional[float]:
    """
    Periodic heartbeat while building ``grep_hie.json`` (module line-grep over RTL).

    Defaults to 30s. ``HIERWALK_HGREP_HEARTBEAT=0`` disables; ``=60`` uses 60s.
    When unset, also honors ``HIERWALK_PW_HEARTBEAT`` if set; otherwise 30s.
    """
    raw = os.environ.get("HIERWALK_HGREP_HEARTBEAT", "").strip().lower()
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return None
    if raw in ("1", "true", "yes", "on"):
        return 30.0
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            return 30.0
    pw = pw_heartbeat_interval_sec()
    if pw is not None:
        return pw
    return 30.0


def connect_jobs_from_env() -> int:
    """``HIERWALK_CONNECT_JOBS`` override for path-walk connect-COI parallelism (0=auto)."""
    raw = os.environ.get("HIERWALK_CONNECT_JOBS", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def preprocess_log_level() -> int:
    """
    Preprocessing tag verbosity for path-walk (``HIERWALK_PP_LOG``).

    0=off, 1=brief (default), 2=all (includes memory-cache hits).
    """
    raw = os.environ.get("HIERWALK_PP_LOG", "").strip().lower()
    if raw in ("0", "off", "false", "no", "disable", "disabled"):
        return 0
    if raw in ("", "1", "brief", "true", "yes", "on"):
        return 1
    if raw in ("2", "all", "verbose"):
        return 2
    try:
        return max(0, min(2, int(raw)))
    except ValueError:
        return 1


def preprocess_log_slow_ms() -> float:
    """Min milliseconds to log ``pp-closure`` at brief level (``HIERWALK_PP_LOG_SLOW_MS``)."""
    raw = os.environ.get("HIERWALK_PP_LOG_SLOW_MS", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 1000.0


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