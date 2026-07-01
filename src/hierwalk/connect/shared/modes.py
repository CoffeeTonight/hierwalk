"""Shared endpoint pairing modes (text + logical)."""

from __future__ import annotations

from hierwalk.models import ConnectEndpoint


def has_port(ep: ConnectEndpoint) -> bool:
    return bool(ep.port_name)


def connect_mode(a: ConnectEndpoint, b: ConnectEndpoint) -> str:
    if has_port(a) and has_port(b):
        return "port-port"
    if has_port(a) or has_port(b):
        return "port-hierarchy"
    return "hierarchy-hierarchy"


def is_ancestor(ancestor: str, path: str) -> bool:
    return path.startswith(ancestor + ".")


# Backward-compatible private aliases used across session + text dedup.
_has_port = has_port
_mode = connect_mode
_is_ancestor = is_ancestor