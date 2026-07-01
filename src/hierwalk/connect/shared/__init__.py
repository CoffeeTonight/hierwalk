"""Shared connect prerequisites: endpoint resolve, request parsing, expand."""

from hierwalk.connect.shared.endpoints import (
    ModuleIndexCacheKey,
    TextGrepIndexCacheKey,
    make_module_index_cache_key,
    net_exists_in_module_fast,
    parse_connect_endpoint,
    resolve_endpoint,
    wire_tail_exists_fast,
)
from hierwalk.connect.shared.expand import (
    aggregate_connect_results,
    expand_check_to_pairs,
)
from hierwalk.connect.shared.modes import connect_mode, has_port, is_ancestor
from hierwalk.connect.shared.request import (
    ConnectivityCheck,
    ConnectivityRequest,
    load_connect_request,
    parse_connect_request_json,
)

__all__ = [
    "ConnectivityCheck",
    "ConnectivityRequest",
    "ModuleIndexCacheKey",
    "TextGrepIndexCacheKey",
    "aggregate_connect_results",
    "connect_mode",
    "expand_check_to_pairs",
    "has_port",
    "is_ancestor",
    "load_connect_request",
    "make_module_index_cache_key",
    "net_exists_in_module_fast",
    "parse_connect_endpoint",
    "parse_connect_request_json",
    "resolve_endpoint",
    "wire_tail_exists_fast",
]