"""Regex-based Verilog instance scan and structural connectivity."""

from hierwalk.connect_request import ConnectivityRequest, load_connect_request
from hierwalk.run_request import RunConfig, load_run_request
from hierwalk.connectivity import (
    ConnectivityBatchResult,
    ConnectivitySession,
    check_connectivity,
    check_connectivity_batch,
    parse_connect_pairs_json,
    run_connectivity_request,
)
from hierwalk.elab import elaborate, flatten
from hierwalk.index import DesignIndex
from hierwalk.models import ConnectResult, FlatRow, SearchHit

__all__ = [
    "ConnectResult",
    "DesignIndex",
    "FlatRow",
    "SearchHit",
    "ConnectivityBatchResult",
    "ConnectivityRequest",
    "RunConfig",
    "ConnectivitySession",
    "check_connectivity",
    "check_connectivity_batch",
    "load_connect_request",
    "load_run_request",
    "parse_connect_pairs_json",
    "run_connectivity_request",
    "elaborate",
    "flatten",
]

__version__ = "0.3.28"