"""Shared connectivity verdict policy for flat-suite verification."""

from __future__ import annotations

# Display / hierarchy-only checks: skip logical-phase positive verdict validation.
CONN_VERDICT_SKIP_IDS = frozenset(
    {
        "zz_list_display",
        "zz_wire_list_display",
        "zz_common_inst_display",
        "zz_common_inst_batch",
        "zz_bridge_d2_bus",
        "zz_collision_nested_same_file",
    }
)