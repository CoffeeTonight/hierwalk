"""Structural connectivity: shared prerequisites, text grep, logical COI."""

from __future__ import annotations

from hierwalk.connect.session import (
    ConnectivityBatchResult,
    ConnectivitySession,
    check_connectivity,
    check_connectivity_batch,
    run_connectivity_request,
)
from hierwalk.connect.shared.request import (
    ConnectivityCheck,
    ConnectivityRequest,
    load_connect_request,
    parse_connect_request_json,
)
from hierwalk.connect.text.pair import connect_pair_text, connect_pair_text_deduped

__all__ = [
    "ConnectivityBatchResult",
    "ConnectivityCheck",
    "ConnectivityRequest",
    "ConnectivitySession",
    "check_connectivity",
    "check_connectivity_batch",
    "connect_pair_text",
    "connect_pair_text_deduped",
    "load_connect_request",
    "parse_connect_request_json",
    "run_connectivity_request",
]