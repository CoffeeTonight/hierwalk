"""Connectivity batch request: JSON spec with checks and options."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hierwalk.connect_expand import (
    CheckExpandMeta,
    EndpointValue,
    build_expand_meta,
    needs_expansion,
    parse_endpoint_elements,
)


@dataclass(frozen=True)
class ConnectivityCheck:
    endpoint_a: str
    endpoint_b: str
    check_id: str = ""
    expand: Optional[CheckExpandMeta] = None


@dataclass(frozen=True)
class ConnectivityRequest:
    """Full connectivity batch request (checks + scan options)."""

    checks: Tuple[ConnectivityCheck, ...]
    top: str = ""
    defines: Dict[str, str] = field(default_factory=dict)
    trace: bool = False
    connect_log: bool = False
    include_ff: bool = False
    strict_generate: bool = False
    over_approximate_if: Optional[bool] = None


_OPTION_KEYS = frozenset(
    {
        "top",
        "defines",
        "trace",
        "connect_trace",
        "connect_log",
        "include_ff",
        "strict_generate",
        "over_approximate_if",
        "ff_barrier",
    }
)


def _endpoint_display(raw: EndpointValue) -> str:
    display, _, _, _ = parse_endpoint_elements(raw)
    return display


def _parse_check_endpoints(
    raw_a: Any,
    raw_b: Any,
    *,
    loop: Any = None,
    map_spec: Any = None,
) -> ConnectivityCheck:
    if isinstance(raw_a, (list, tuple)):
        endpoint_a: EndpointValue = tuple(str(x).strip() for x in raw_a)
    else:
        endpoint_a = str(raw_a).strip()
    if isinstance(raw_b, (list, tuple)):
        endpoint_b: EndpointValue = tuple(str(x).strip() for x in raw_b)
    else:
        endpoint_b = str(raw_b).strip()
    if not endpoint_a or (isinstance(endpoint_a, str) and not endpoint_a):
        raise ValueError("endpoint a must be non-empty")
    if not endpoint_b or (isinstance(endpoint_b, str) and not endpoint_b):
        raise ValueError("endpoint b must be non-empty")

    expand = build_expand_meta(
        endpoint_a,
        endpoint_b,
        loop=loop,
        map_spec=map_spec,
    )
    if not needs_expansion(expand):
        expand = None
    return ConnectivityCheck(
        _endpoint_display(endpoint_a),
        _endpoint_display(endpoint_b),
        expand=expand,
    )


def _parse_check_item(item: Any, *, index: int) -> ConnectivityCheck:
    check_id = ""
    if isinstance(item, (list, tuple)):
        if len(item) != 2:
            raise ValueError(f"checks[{index}] must have exactly two endpoints")
        chk = _parse_check_endpoints(item[0], item[1])
        if check_id:
            return ConnectivityCheck(
                chk.endpoint_a,
                chk.endpoint_b,
                check_id=check_id,
                expand=chk.expand,
            )
        return chk
    if isinstance(item, dict):
        check_id = str(item.get("id") or item.get("name") or "").strip()
        loop = item.get("loop", item.get("bind"))
        map_spec = item.get("map")
        for a_key, b_key in (
            ("a", "b"),
            ("from", "to"),
            ("endpoint_a", "endpoint_b"),
            ("src", "dst"),
        ):
            if a_key in item and b_key in item:
                chk = _parse_check_endpoints(
                    item[a_key],
                    item[b_key],
                    loop=loop,
                    map_spec=map_spec,
                )
                return ConnectivityCheck(
                    chk.endpoint_a,
                    chk.endpoint_b,
                    check_id=check_id,
                    expand=chk.expand,
                )
        raise ValueError(
            f"checks[{index}] needs a/b, from/to, endpoint_a/endpoint_b, or src/dst"
        )
    raise ValueError(f"checks[{index}] must be [a, b] or an object")


def _parse_options(data: Mapping[str, Any]) -> Dict[str, Any]:
    defines_raw = data.get("defines") or {}
    if not isinstance(defines_raw, dict):
        raise ValueError("'defines' must be an object")
    defines = {str(k): str(v) for k, v in defines_raw.items()}

    over_approx = data.get("over_approximate_if")
    if over_approx is not None and not isinstance(over_approx, bool):
        raise ValueError("'over_approximate_if' must be boolean or null")

    include_ff = data.get("include_ff", False)
    if "ff_barrier" in data:
        include_ff = not bool(data["ff_barrier"])

    trace = bool(data.get("connect_trace", data.get("trace", False)))
    connect_log = bool(data.get("connect_log", False))

    return {
        "top": str(data.get("top") or "").strip(),
        "defines": defines,
        "trace": trace or connect_log,
        "connect_log": connect_log,
        "include_ff": bool(include_ff),
        "strict_generate": bool(data.get("strict_generate", False)),
        "over_approximate_if": over_approx,
    }


def parse_connect_request_json(data: Any) -> ConnectivityRequest:
    """
    Parse a connectivity request JSON document.

    Minimal (pairs only)::

        [["top.a", "top.b"]]

    Full spec::

        {
          "top": "stress_top",
          "defines": {"STRESS_USE_IN": "1"},
          "include_ff": false,
          "connect_trace": false,
          "checks": [
            {"id": "clk", "a": "top.clk", "b": "top.u0.clk"},
            {"id": "bad", "a": "top.u_nope.x", "b": "top.clk"}
          ]
        }
    """
    if isinstance(data, list):
        checks = tuple(_parse_check_item(item, index=i) for i, item in enumerate(data))
        return ConnectivityRequest(checks=checks)

    if not isinstance(data, dict):
        raise ValueError("connect request JSON must be an object or array")

    items: Sequence[Any]
    for key in ("checks", "pairs", "connections"):
        if key in data:
            items = data[key]
            break
    else:
        raise ValueError("request object needs 'checks', 'pairs', or 'connections'")

    if not isinstance(items, list):
        raise ValueError("checks/pairs must be a JSON array")

    checks = tuple(_parse_check_item(item, index=i) for i, item in enumerate(items))
    if not checks:
        raise ValueError("request contains no checks")

    opts = _parse_options(data)
    return ConnectivityRequest(checks=checks, **opts)


def try_parse_connect_request_json(data: Any) -> Optional[ConnectivityRequest]:
    """Parse a connect document when ``checks``/``pairs`` are present; else ``None``."""
    if isinstance(data, list):
        if not data:
            return None
        try:
            return parse_connect_request_json(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    if not any(key in data for key in ("checks", "pairs", "connections")):
        return None
    try:
        return parse_connect_request_json(data)
    except ValueError:
        return None


def load_connect_request(path: Union[str, Path]) -> ConnectivityRequest:
    p = Path(path)
    text = p.read_text(encoding="utf-8").lstrip()
    if p.suffix.lower() == ".json" or text.startswith(("{", "[")):
        return parse_connect_request_json(json.loads(text))
    pairs = _load_connect_pairs_text(p)
    return ConnectivityRequest(
        checks=tuple(
            ConnectivityCheck(a, b) for a, b in pairs
        ),
    )


def _load_connect_pairs_text(path: Path) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "\t" in line:
            cols = [c.strip() for c in line.split("\t") if c.strip()]
        else:
            cols = line.split()
        if len(cols) < 2:
            raise ValueError(f"expected two endpoints per line: {raw!r}")
        pairs.append((cols[0], cols[1]))
    if not pairs:
        raise ValueError(f"no endpoint pairs in {path}")
    return pairs


def connect_request_to_json(req: ConnectivityRequest, *, indent: int = 2) -> str:
    payload: Dict[str, Any] = {
        "top": req.top,
        "defines": dict(req.defines),
        "include_ff": req.include_ff,
        "connect_trace": req.trace,
        "strict_generate": req.strict_generate,
        "checks": [],
    }
    if req.connect_log:
        payload["connect_log"] = True
    if req.over_approximate_if is not None:
        payload["over_approximate_if"] = req.over_approximate_if
    def _endpoint_json(chk: ConnectivityCheck, side: str) -> Any:
        if chk.expand is None:
            return chk.endpoint_a if side == "a" else chk.endpoint_b
        elements = chk.expand.elements_a if side == "a" else chk.expand.elements_b
        if len(elements) > 1:
            if chk.expand.map_kind == "concat":
                return "{" + ", ".join(elements) + "}"
            return list(elements)
        return elements[0]

    for chk in req.checks:
        item: Dict[str, Any] = {"a": _endpoint_json(chk, "a"), "b": _endpoint_json(chk, "b")}
        if chk.check_id:
            item["id"] = chk.check_id
        if chk.expand is not None:
            if chk.expand.loop:
                loop_out: Dict[str, Any] = {}
                for key, values in chk.expand.loop:
                    loop_out[key] = _loop_value_for_json(values)
                item["loop"] = loop_out
            map_out = _map_options_for_json(chk.expand)
            if map_out:
                item["map"] = map_out
        payload["checks"].append(item)
    return json.dumps(payload, indent=indent) + "\n"


def _loop_value_for_json(values: Tuple[str, ...]) -> Any:
    if not values:
        return []
    if all(v.isdigit() or (v.startswith("-") and v[1:].isdigit()) for v in values):
        nums = [int(v) for v in values]
        if len(nums) > 1:
            step = nums[1] - nums[0]
            if step != 0 and all(nums[i] - nums[i - 1] == step for i in range(1, len(nums))):
                return f"{nums[0]}:{nums[-1]}"
        return [int(v) for v in values]
    if all(v.isalnum() or v in "-_" for v in values):
        return ",".join(values)
    return list(values)


def _map_options_for_json(expand: CheckExpandMeta) -> Dict[str, Any]:
    if expand.map_kind == "waypoint-fanout":
        out: Dict[str, Any] = {"kind": "waypoint-fanout"}
        if len(expand.path_kinds) == 1:
            if expand.path_kinds[0] != "comb":
                out["path_kind"] = expand.path_kinds[0]
        else:
            out["path_kind"] = list(expand.path_kinds)
        if getattr(expand, "direction", "fanout") != "fanout":
            out["direction"] = expand.direction
        return out
    out = {}
    if expand.bit_align != "lsb":
        out["bit_align"] = expand.bit_align
    if expand.fanout_mode != "all":
        out["mode"] = expand.fanout_mode
    return out


def write_connect_request(path: Union[str, Path], req: ConnectivityRequest) -> None:
    Path(path).write_text(connect_request_to_json(req), encoding="utf-8")