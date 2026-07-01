"""Expand rich connectivity check endpoints into 1:1 endpoint pairs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from hierwalk.connect.logical.scan import (
    _expand_concat_elements,
    _is_const_literal,
    _split_concat_top_level,
)
from hierwalk.models import ConnectEndpoint, ConnectResult

EndpointValue = Union[str, Sequence[str]]

_LOOP_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")
_BUS_RANGE_RE = re.compile(r"^(.*)\[([^\]]+)\]$")


@dataclass(frozen=True)
class CheckExpandMeta:
    """Parsed expansion metadata retained for round-trip and lazy-scope."""

    loop: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    map_kind: str = ""
    bit_align: str = "lsb"
    fanout_mode: str = "all"
    path_kinds: Tuple[str, ...] = ("comb",)
    direction: str = "fanout"
    trace_interior: bool = False
    full_path_kinds: bool = False
    elements_a: Tuple[str, ...] = ()
    elements_b: Tuple[str, ...] = ()
    list_a: bool = False
    list_b: bool = False
    concat_a: bool = False
    concat_b: bool = False

    @property
    def path_kind(self) -> str:
        return self.path_kinds[0]


@dataclass(frozen=True)
class ExpandedPair:
    endpoint_a: str
    endpoint_b: str
    sub_id: str = ""


def _is_concat_string(value: str) -> bool:
    text = value.strip()
    return text.startswith("{") and text.endswith("}")


def _parse_concat_string(value: str) -> Tuple[str, ...]:
    inner = value.strip()[1:-1].strip()
    if not inner:
        raise ValueError("concat endpoint must contain at least one element")
    return tuple(_expand_concat_elements(inner))


def _normalize_list(value: Sequence[Any]) -> Tuple[str, ...]:
    out: List[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise ValueError("endpoint list elements must be non-empty strings")
        out.append(text)
    if not out:
        raise ValueError("endpoint list must not be empty")
    return tuple(out)


def _list_has_literal(elements: Sequence[str]) -> bool:
    return any(_is_const_literal(el) for el in elements)


def _parse_loop_values(spec: Any) -> Tuple[str, ...]:
    if isinstance(spec, (list, tuple)):
        out = tuple(str(v).strip() for v in spec if str(v).strip())
        if not out:
            raise ValueError("loop list must not be empty")
        return out
    if isinstance(spec, str):
        text = spec.strip()
        if not text:
            raise ValueError("loop value must be non-empty")
        if ":" in text and not text.startswith("{"):
            return _parse_range_text(text)
        if "," in text:
            return tuple(p.strip() for p in text.split(",") if p.strip())
        return (text,)
    if isinstance(spec, dict):
        if "enum" in spec:
            return _parse_loop_values(spec["enum"])
        if "range" in spec:
            return _parse_range_text(str(spec["range"]).strip())
    raise ValueError("loop entry must be a string, array, or legacy {range|enum} object")


def _parse_range_text(text: str) -> Tuple[str, ...]:
    if ":" not in text:
        return (text,)
    msb_s, lsb_s = text.split(":", 1)
    try:
        msb = int(msb_s.strip())
        lsb = int(lsb_s.strip())
    except ValueError as exc:
        raise ValueError(f"invalid loop range {text!r}") from exc
    if msb >= lsb:
        return tuple(str(i) for i in range(msb, lsb - 1, -1))
    return tuple(str(i) for i in range(msb, lsb + 1))


def _parse_loop_map(loop: Any) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(loop, dict):
        raise ValueError("'loop' must be an object")
    out: Dict[str, Tuple[str, ...]] = {}
    for key, spec in loop.items():
        name = str(key).strip()
        if not name:
            raise ValueError("loop keys must be non-empty")
        out[name] = _parse_loop_values(spec)
    return out


def _apply_loop_placeholders(template: str, assignment: Mapping[str, str]) -> str:
    """Replace ``{I}{J}`` with concatenated values (e.g. I=x, J=A -> xA)."""

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in assignment:
            raise ValueError(f"unbound loop placeholder {{{key}}} in {template!r}")
        return assignment[key]

    return _LOOP_PLACEHOLDER_RE.sub(repl, template)


def _loop_assignments(loop: Mapping[str, Tuple[str, ...]]) -> List[Dict[str, str]]:
    if not loop:
        return [{}]
    keys = list(loop.keys())
    values = [loop[k] for k in keys]
    out: List[Dict[str, str]] = [{}]

    def expand(idx: int, cur: Dict[str, str]) -> None:
        if idx >= len(keys):
            out.append(dict(cur))
            return
        key = keys[idx]
        for val in values[idx]:
            nxt = dict(cur)
            nxt[key] = val
            expand(idx + 1, nxt)

    expand(0, {})
    return out[1:]


def _expand_looped_endpoint(
    value: str,
    loop: Mapping[str, Tuple[str, ...]],
) -> Tuple[str, ...]:
    if not loop:
        return (value,)
    templates = [_apply_loop_placeholders(value, a) for a in _loop_assignments(loop)]
    return tuple(templates)


def _parse_path_kinds(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ("comb",)
    if isinstance(raw, str):
        path_kind = raw.strip().lower()
        if path_kind not in ("comb", "ff"):
            raise ValueError("map.path_kind must be 'comb' or 'ff'")
        return (path_kind,)
    if isinstance(raw, (list, tuple)):
        if not raw:
            raise ValueError("map.path_kind array must not be empty")
        seen: List[str] = []
        for item in raw:
            path_kind = str(item).strip().lower()
            if path_kind not in ("comb", "ff"):
                raise ValueError(
                    "map.path_kind array entries must be 'comb' and/or 'ff'"
                )
            if path_kind not in seen:
                seen.append(path_kind)
        return tuple(seen)
    raise ValueError("map.path_kind must be a string or array")


def _parse_map_spec(
    raw: Any,
) -> Tuple[str, str, str, Tuple[str, ...], str, bool, bool]:
    if raw is None:
        return "", "lsb", "all", ("comb",), "fanout", False, False
    if not isinstance(raw, dict):
        raise ValueError("'map' must be an object")
    kind = str(raw.get("kind") or raw.get("mode_kind") or "").strip().lower()
    bit_align = str(raw.get("bit_align") or "lsb").strip().lower()
    if bit_align not in ("msb", "lsb"):
        raise ValueError("map.bit_align must be 'msb' or 'lsb'")
    mode = str(raw.get("mode") or "all").strip().lower()
    if mode not in ("all", "any"):
        raise ValueError("map.mode must be 'all' or 'any'")
    path_kinds = _parse_path_kinds(raw.get("path_kind"))
    direction = str(raw.get("direction") or "fanout").strip().lower()
    if direction not in ("fanout", "both"):
        raise ValueError("map.direction must be 'fanout' or 'both'")
    trace_interior = bool(raw.get("trace_interior", False))
    full_path_kinds = bool(raw.get("full_path_kinds", False))
    return kind, bit_align, mode, path_kinds, direction, trace_interior, full_path_kinds


def _bus_range_indices(inner: str, *, msb_first: bool = False) -> Optional[List[int]]:
    inner = inner.strip()
    if ":" not in inner:
        try:
            return [int(inner)]
        except ValueError:
            return None
    msb_s, lsb_s = inner.split(":", 1)
    try:
        msb = int(msb_s.strip())
        lsb = int(lsb_s.strip())
    except ValueError:
        return None
    if msb >= lsb:
        indices = list(range(lsb, msb + 1))
    else:
        indices = list(range(msb, lsb + 1))
    if msb_first:
        indices.reverse()
    return indices


def _expand_bus_endpoint(
    spec: str,
    *,
    msb_first: bool = False,
) -> Optional[Tuple[str, ...]]:
    m = _BUS_RANGE_RE.match(spec.strip())
    if m is None:
        return None
    base, inner = m.group(1), m.group(2)
    indices = _bus_range_indices(inner, msb_first=msb_first)
    if indices is None:
        return None
    return tuple(f"{base}[{i}]" for i in indices)


def _ordered_bits(specs: Sequence[str], *, bit_align: str) -> List[str]:
    expanded: List[str] = []
    msb_first = bit_align == "msb"
    for spec in specs:
        bus = _expand_bus_endpoint(spec, msb_first=msb_first)
        if bus is not None:
            expanded.extend(bus)
        else:
            expanded.append(spec)
    return expanded


def _concat_signal_pairs(
    elements: Sequence[str],
    other: str,
    *,
    bit_align: str,
) -> List[ExpandedPair]:
    bus = _expand_bus_endpoint(other, msb_first=True)
    other_bits = list(bus) if bus is not None else [other]
    pairs: List[ExpandedPair] = []
    bit_idx = 0
    for pos, el in enumerate(elements):
        if _is_const_literal(el):
            continue
        if bit_idx >= len(other_bits):
            raise ValueError(
                f"concat has more signal elements than bus bits ({len(other_bits)})"
            )
        pairs.append(
            ExpandedPair(
                endpoint_a=el,
                endpoint_b=other_bits[bit_idx],
                sub_id=f"[{pos}]",
            )
        )
        bit_idx += 1
    if bit_idx < len(other_bits):
        raise ValueError(
            f"concat has fewer signal elements ({bit_idx}) than bus bits ({len(other_bits)})"
        )
    return pairs


def _zip_array_pairs(
    left: Sequence[str],
    right: Sequence[str],
    *,
    bit_align: str,
    prefix: str = "",
) -> List[ExpandedPair]:
    del prefix  # check_id prefix applied by ConnectivitySession / artifacts
    left_bits = _ordered_bits(left, bit_align=bit_align)
    right_bits = _ordered_bits(right, bit_align=bit_align)
    if len(left_bits) != len(right_bits):
        raise ValueError(
            f"array map length mismatch: {len(left_bits)} vs {len(right_bits)}"
        )
    return [
        ExpandedPair(
            endpoint_a=left_bits[i],
            endpoint_b=right_bits[i],
            sub_id=f"[{i}]",
        )
        for i in range(len(left_bits))
    ]


def _fanout_pairs(
    source: str,
    sinks: Sequence[str],
) -> List[ExpandedPair]:
    return [
        ExpandedPair(endpoint_a=source, endpoint_b=sink, sub_id=f"->{i}")
        for i, sink in enumerate(sinks)
    ]


def _reject_list_literals(
    elements: Tuple[str, ...],
    *,
    is_list: bool,
    is_concat: bool,
    side: str,
) -> None:
    if is_list and not is_concat and _list_has_literal(elements):
        raise ValueError(
            f"endpoint {side}: Verilog literals require {{…}} concat form, not […] "
            "(concat enforces MSB-first bit order; [] uses index zip only)"
        )


def _infer_map_kind(
    elements_a: Tuple[str, ...],
    elements_b: Tuple[str, ...],
    map_kind: str,
    *,
    list_a: bool,
    list_b: bool,
    concat_a: bool,
    concat_b: bool,
    has_loop: bool,
) -> str:
    if map_kind == "concat":
        if not (concat_a or concat_b):
            raise ValueError(
                "map.kind concat requires at least one {…} concat endpoint"
            )
        return "concat"
    if map_kind == "waypoint-fanout":
        return "waypoint-fanout"
    if map_kind:
        return map_kind
    if concat_a or concat_b:
        return "concat"
    len_a = len(elements_a)
    len_b = len(elements_b)
    if list_a and list_b:
        return "array"
    if list_b and len_a == 1:
        return "fanout"
    if list_a and len_b == 1:
        bus = _expand_bus_endpoint(elements_b[0])
        if bus is not None and len(bus) == len_a:
            return "array"
        return "fanout"
    if has_loop and (len_a > 1 or len_b > 1):
        return "array"
    if len_a > 1 and len_b > 1:
        return "array"
    return "array"


def parse_endpoint_elements(
    raw: EndpointValue,
) -> Tuple[str, Tuple[str, ...], bool, bool]:
    """
    Return (display_spec, elements, is_list_form, is_concat_form).

    Scalar strings stay single-element; ``{…}`` concat and ``[…]`` lists expand.
    """
    if isinstance(raw, (list, tuple)):
        elements = _normalize_list(raw)
        display = "[" + ", ".join(elements) + "]"
        return display, elements, True, False
    text = str(raw).strip()
    if not text:
        raise ValueError("endpoint must be a non-empty string or list")
    if _is_concat_string(text):
        elements = _parse_concat_string(text)
        return text, elements, True, True
    return text, (text,), False, False


def parse_list_display_spec(spec: str) -> Optional[Tuple[str, ...]]:
    """
    Parse a JSON list display string like ``[top.a, top.b]`` into endpoint paths.

    Returns ``None`` when *spec* is a plain scalar path (not bracket-wrapped).
    """
    text = str(spec or "").strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    inner = text[1:-1].strip()
    if not inner:
        return ()
    parts = tuple(part.strip() for part in inner.split(",") if part.strip())
    return parts if parts else ()


def hierarchy_endpoint_specs(
    spec: str,
    *,
    inst_path: str = "",
    port_name: str = "",
    port_found: bool = False,
) -> Tuple[str, ...]:
    """Return one or more hierarchy path specs (never a bracket-wrapped display blob)."""
    listed = parse_list_display_spec(spec)
    if listed is not None:
        return listed
    text = str(spec or "").strip()
    if text:
        return (text,)
    if port_name and not port_found:
        full = f"{inst_path}.{port_name}" if inst_path else port_name
        if full:
            return (full,)
    if inst_path:
        return (inst_path,)
    return ()


def _loop_expand_elements(
    elements: Tuple[str, ...],
    loop_map: Mapping[str, Tuple[str, ...]],
) -> Tuple[str, ...]:
    if not loop_map or not elements:
        return elements
    template = elements[0]
    if any(f"{{{key}}}" in template for key in loop_map):
        return _expand_looped_endpoint(template, loop_map)
    return elements


def build_expand_meta(
    raw_a: EndpointValue,
    raw_b: EndpointValue,
    *,
    loop: Any = None,
    map_spec: Any = None,
) -> CheckExpandMeta:
    _, elements_a, list_a, concat_a = parse_endpoint_elements(raw_a)
    _, elements_b, list_b, concat_b = parse_endpoint_elements(raw_b)

    loop_map = _parse_loop_map(loop) if loop else {}
    (
        map_kind,
        bit_align,
        fanout_mode,
        path_kinds,
        direction,
        trace_interior,
        full_path_kinds,
    ) = _parse_map_spec(map_spec)

    final_a = _loop_expand_elements(elements_a, loop_map)
    final_b = _loop_expand_elements(elements_b, loop_map)

    _reject_list_literals(final_a, is_list=list_a, is_concat=concat_a, side="a")
    _reject_list_literals(final_b, is_list=list_b, is_concat=concat_b, side="b")

    kind = _infer_map_kind(
        final_a,
        final_b,
        map_kind,
        list_a=list_a,
        list_b=list_b,
        concat_a=concat_a,
        concat_b=concat_b,
        has_loop=bool(loop_map),
    )

    return CheckExpandMeta(
        loop=tuple(loop_map.items()),
        map_kind=kind,
        bit_align=bit_align,
        fanout_mode=fanout_mode,
        path_kinds=path_kinds,
        direction=direction,
        trace_interior=trace_interior,
        full_path_kinds=full_path_kinds,
        elements_a=final_a,
        elements_b=final_b,
        list_a=list_a,
        list_b=list_b,
        concat_a=concat_a,
        concat_b=concat_b,
    )


def needs_expansion(meta: Optional[CheckExpandMeta]) -> bool:
    if meta is None:
        return False
    if meta.map_kind == "waypoint-fanout":
        return True
    if meta.loop:
        return True
    if meta.map_kind in ("array", "fanout", "concat"):
        if len(meta.elements_a) > 1 or len(meta.elements_b) > 1:
            return True
        if meta.map_kind == "concat":
            return True
    return False


def expand_check_to_pairs(
    endpoint_a: str,
    endpoint_b: str,
    *,
    check_id: str = "",
    expand: Optional[CheckExpandMeta] = None,
) -> List[ExpandedPair]:
    if expand is not None and expand.map_kind == "waypoint-fanout":
        raise ValueError(
            "waypoint-fanout checks are handled by ConnectivitySession.check, "
            "not expand_check_to_pairs"
        )
    if expand is None or not needs_expansion(expand):
        return [
            ExpandedPair(
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                sub_id="",
            )
        ]

    elements_a = expand.elements_a
    elements_b = expand.elements_b
    kind = expand.map_kind
    bit_align = expand.bit_align

    if kind == "concat":
        if len(elements_b) == 1:
            return _concat_signal_pairs(
                elements_a,
                elements_b[0],
                bit_align=bit_align,
            )
        if len(elements_a) == 1:
            pairs = _concat_signal_pairs(
                elements_b,
                elements_a[0],
                bit_align=bit_align,
            )
            return [
                ExpandedPair(
                    endpoint_a=p.endpoint_b,
                    endpoint_b=p.endpoint_a,
                    sub_id=p.sub_id,
                )
                for p in pairs
            ]
        raise ValueError(
            "concat map needs one {…} element list and one bus/scalar endpoint"
        )

    if kind == "fanout":
        if len(elements_a) == 1 and len(elements_b) > 1:
            return _fanout_pairs(elements_a[0], elements_b)
        if len(elements_b) == 1 and len(elements_a) > 1:
            pairs = _fanout_pairs(elements_b[0], elements_a)
            return [
                ExpandedPair(endpoint_a=p.endpoint_b, endpoint_b=p.endpoint_a, sub_id=p.sub_id)
                for p in pairs
            ]
        raise ValueError("fanout map needs one scalar endpoint and a list on the other side")

    if kind == "array":
        if len(elements_a) > 1 and len(elements_b) == 1:
            right_bits = _ordered_bits(elements_b, bit_align=bit_align)
            if len(right_bits) == len(elements_a):
                return [
                    ExpandedPair(
                        endpoint_a=left,
                        endpoint_b=right_bits[i],
                        sub_id=f"[{i}]",
                    )
                    for i, left in enumerate(elements_a)
                ]
            right = elements_b[0]
            return [
                ExpandedPair(
                    endpoint_a=left,
                    endpoint_b=right,
                    sub_id=f"[{i}]",
                )
                for i, left in enumerate(elements_a)
            ]
        if len(elements_b) > 1 and len(elements_a) == 1:
            left_bits = _ordered_bits(elements_a, bit_align=bit_align)
            if len(left_bits) == len(elements_b):
                return [
                    ExpandedPair(
                        endpoint_a=left_bits[i],
                        endpoint_b=right,
                        sub_id=f"[{i}]",
                    )
                    for i, right in enumerate(elements_b)
                ]
            left = elements_a[0]
            return [
                ExpandedPair(
                    endpoint_a=left,
                    endpoint_b=right,
                    sub_id=f"[{i}]",
                )
                for i, right in enumerate(elements_b)
            ]
        return _zip_array_pairs(
            elements_a,
            elements_b,
            bit_align=bit_align,
            prefix=check_id or "pair",
        )

    return [
        ExpandedPair(
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            sub_id="",
        )
    ]


def endpoint_specs_from_expand(expand: Optional[CheckExpandMeta]) -> List[str]:
    if expand is None:
        return []
    specs: List[str] = []
    for el in expand.elements_a + expand.elements_b:
        if _is_const_literal(el):
            continue
        if _LOOP_PLACEHOLDER_RE.search(el):
            loop_map = dict(expand.loop)
            specs.extend(_expand_looped_endpoint(el, loop_map))
        else:
            bus = _expand_bus_endpoint(el)
            if bus is not None:
                specs.extend(bus)
            else:
                specs.append(el)
    return specs


def _placeholder_endpoint(spec: str, inst_path: str) -> ConnectEndpoint:
    return ConnectEndpoint(spec=spec, inst_path=inst_path)


def aggregate_connect_results(
    endpoint_a: str,
    endpoint_b: str,
    sub_results: Sequence[ConnectResult],
    *,
    check_id: str = "",
    fanout_mode: str = "all",
) -> ConnectResult:
    if not sub_results:
        return ConnectResult(
            _placeholder_endpoint(endpoint_a, ""),
            _placeholder_endpoint(endpoint_b, ""),
            False,
            "unknown",
            errors=["no expanded sub-checks"],
            check_id=check_id,
        )
    if len(sub_results) == 1 and not sub_results[0].check_id:
        single = sub_results[0]
        return ConnectResult(
            single.endpoint_a,
            single.endpoint_b,
            single.connected,
            single.mode,
            hops=single.hops,
            errors=list(single.errors),
            note=single.note,
            check_id=check_id or single.check_id,
            sub_results=(),
        )

    connected = (
        all(r.connected for r in sub_results)
        if fanout_mode == "all"
        else any(r.connected for r in sub_results)
    )
    errors: List[str] = []
    for r in sub_results:
        errors.extend(r.errors)
    modes = {r.mode for r in sub_results}
    mode = modes.pop() if len(modes) == 1 else "expanded"
    failed = sum(1 for r in sub_results if not r.connected)
    note = f"expanded {len(sub_results)} sub-checks ({fanout_mode}); failed={failed}"
    inst_a = sub_results[0].endpoint_a.inst_path or endpoint_a.split(".", 1)[0]
    inst_b = sub_results[0].endpoint_b.inst_path or endpoint_b.split(".", 1)[0]
    return ConnectResult(
        _placeholder_endpoint(endpoint_a, inst_a),
        _placeholder_endpoint(endpoint_b, inst_b),
        connected,
        mode,
        errors=errors,
        note=note,
        check_id=check_id,
        sub_results=tuple(sub_results),
    )