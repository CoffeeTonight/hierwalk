"""Text-conn: coarse RHS name grep (not propagation)."""

from __future__ import annotations

from hierwalk.connect.text.dedup import text_dedup_key
from hierwalk.connect.text.index import TextGrepCache, TextGrepIndex, build_text_grep_index
from hierwalk.connect.text.pair import connect_pair_text, connect_pair_text_deduped
from hierwalk.connect.text.walk import (
    TextWalkSessionCaches,
    bidirectional_text_grep,
    forward_text_grep_to_scope,
)

__all__ = [
    "TextGrepCache",
    "TextGrepIndex",
    "TextWalkSessionCaches",
    "bidirectional_text_grep",
    "build_text_grep_index",
    "connect_pair_text",
    "connect_pair_text_deduped",
    "forward_text_grep_to_scope",
    "text_dedup_key",
]