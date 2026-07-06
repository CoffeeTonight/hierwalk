"""Shared connectivity verdict policy for flat-suite verification."""

from __future__ import annotations

from hierwalk.zigzag_annex_gen import (
    matrix_hierarchy_check_id,
    vuln_annex_verdict_skip_ids,
    vuln_logical_only_negative_ids,
)

# Display / hierarchy-only checks: skip logical-phase positive verdict validation.
CONN_VERDICT_SKIP_IDS = frozenset(
    {
        "zz_list_display",
        "zz_hier_array",
        "zz_wire_list_display",
        "zz_common_inst_display",
        "zz_common_inst_batch",
        "zz_bridge_d2_bus",
        "zz_collision_nested_same_file",
        matrix_hierarchy_check_id(),
        *vuln_annex_verdict_skip_ids(),
    }
)

# Negative checks where text-conn bloom-pass is expected; verdict only on logical-conn.
CONN_LOGICAL_ONLY_NEGATIVE_IDS = frozenset(vuln_logical_only_negative_ids())