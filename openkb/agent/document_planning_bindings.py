"""Bind request-local location aliases to immutable, actually supplied source slices."""

from __future__ import annotations

import json
import re
from copy import deepcopy

from openkb.agent.document_plan import range_intervals
from openkb.locks import atomic_write_json
from openkb.sources import content_id, valid_id

_ALIAS = re.compile(r"@[A-Za-z_][A-Za-z0-9_]*:[A-Za-z0-9_]+")
_RANGE = re.compile(r"[\s`]*(?:[-–—~～]|至|到)[\s`]*")
_IDENTITY = ("source_id", "version_id", "parse_id")


def _path(store, identity):
    return store.owned_path(
        store.root / "compilation" / "planning-bindings" / (valid_id(identity) + ".json")
    )


def capture_request(messages, source, parsed, window, store):
    """Persist the real WireMessages mapping; never reconstruct aliases from order."""
    payload = json.loads(messages[-1]["content"])
    evidence = payload["evidence"]
    inverse = messages.inverse
    identity = dict(zip(_IDENTITY, (source.source_id, source.id, parsed.id), strict=True))
    for key, expected in identity.items():
        if inverse.get(evidence.get(key), evidence.get(key)) != expected:
            raise ValueError("Planning request source identity mismatch")
    aliases = []
    for block in evidence["blocks"]:
        order = block["order"]
        if type(order) is not int or not 0 <= order < len(parsed.blocks):
            raise ValueError("Planning request block order mismatch")
        block_id = inverse.get(block["id"], block["id"])
        if block_id != parsed.blocks[order].id:
            raise ValueError("Planning request block identity mismatch")
        reference = block.get("reference") or {}
        for key, expected in {**identity, "block_id": block_id}.items():
            if key in reference and inverse.get(reference[key], reference[key]) != expected:
                raise ValueError("Planning request evidence identity mismatch")
        extent = block["text_extent"]
        if not block["text"]:
            continue
        selected = {
            "block_index": order,
            "start_char": extent["start_char"],
            "end_char": extent["end_char"],
        }
        range_intervals(selected, parsed, "planning request slice")
        if selected["end_char"] - selected["start_char"] != len(block["text"]):
            raise ValueError("Planning request text extent mismatch")
        aliases.append({"alias": block["id"], "block_id": block_id, "range": selected})
    record = {
        "protocol": "planning-request-binding-v1",
        **identity,
        "request": content_id(list(messages)),
        "window": content_id(window),
        "aliases": aliases,
    }
    key = content_id(record)
    atomic_write_json(_path(store, key), record)
    return {"id": key, **record}


def load_binding(store, key, source, parsed):
    """A missing or corrupt saved binding is an integrity failure, not bad model text."""
    record = json.loads(_path(store, key).read_text(encoding="utf-8"))
    if content_id(record) != key:
        raise ValueError("Planning request binding changed")
    validate_binding(record, source.source_id, source.id, parsed)
    return {"id": key, **record}


def validate_binding(record, source_id, version_id, parsed):
    if not isinstance(record, dict) or record.get("protocol") != "planning-request-binding-v1":
        raise ValueError("Invalid planning request binding")
    if tuple(record.get(key) for key in _IDENTITY) != (source_id, version_id, parsed.id):
        raise ValueError("Planning request binding source mismatch")
    for row in record.get("aliases", []):
        for order, _, _ in range_intervals(row["range"], parsed, "bound request slice"):
            if row["block_id"] != parsed.blocks[order].id:
                raise ValueError("Planning request binding block mismatch")


def _selection(alias, binding):
    rows = [row["range"] for row in binding["aliases"] if row["alias"] == alias]
    unique = [value for i, value in enumerate(rows) if value not in rows[:i]]
    return unique[0] if len(unique) == 1 else None


def _span(first, last, binding, parsed):
    left, right = _selection(first, binding), _selection(last, binding)
    if left is None or right is None or left["block_index"] > right["block_index"]:
        return None
    selected = []
    for order in range(left["block_index"], right["block_index"] + 1):
        start = left["start_char"] if order == left["block_index"] else 0
        end = right["end_char"] if order == right["block_index"] else parsed.blocks[order].chars
        supplied = sorted(
            (row["range"]["start_char"], row["range"]["end_char"])
            for row in binding["aliases"]
            if row["range"]["block_index"] == order
        )
        reached = start
        for a, b in supplied:
            if a <= reached:
                reached = max(reached, b)
        if reached < end:
            return None
        selected.append({"block_index": order, "start_char": start, "end_char": end})
    return selected


def _resolve_aliases(raw, binding, parsed):
    if isinstance(raw, dict) and {"from_block", "through_block"} <= raw.keys():
        text = str(raw["from_block"]) + "–" + str(raw["through_block"])
    else:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    tokens = list(_ALIAS.finditer(text))
    ranges, unresolved = [], []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if i + 1 < len(tokens) and _RANGE.fullmatch(text[token.end() : tokens[i + 1].start()]):
            last = tokens[i + 1]
            selected = _span(token.group(), last.group(), binding, parsed)
            if selected is None:
                unresolved.append(text[token.start() : last.end()])
            else:
                ranges.extend(row for row in selected if row not in ranges)
            i += 2
            continue
        selected = _selection(token.group(), binding)
        if selected is None:
            unresolved.append(token.group())
        else:
            if isinstance(raw, dict) and "block" in raw and "start_char" in raw:
                start, end = raw.get("start_char"), raw.get("end_char")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not selected["start_char"] <= start < end <= selected["end_char"]
                ):
                    unresolved.append(token.group())
                    i += 1
                    continue
                selected = {**selected, "start_char": start, "end_char": end}
            if selected not in ranges:
                ranges.append(selected)
        i += 1
    return ranges, unresolved


def bind_hints(hints, binding, parsed, notes):
    result = []
    for hint in hints:
        value = hint["value"]
        if isinstance(value, dict) and value.get("format") == "bound-location-v1":
            # This representation is program-owned; model text cannot supply it.
            result.append({**hint, "value": {"untrusted_location": value}})
            notes.append("模型提供的程序定位字段未作为读取授权")
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if not _ALIAS.search(text):
            result.append(hint)
            continue
        if binding is None:
            notes.append("请求内短 ID 未绑定：缺少原请求映射")
            result.append({**hint, "value": {"unbound_request_location": value}})
            continue
        ranges, unresolved = _resolve_aliases(value, binding, parsed)
        origin = {key: binding[key] for key in (*_IDENTITY, "request", "window")}
        origin["binding"] = binding["id"]
        result.append(
            {
                **hint,
                "value": {
                    "format": "bound-location-v1",
                    "raw": value,
                    "ranges": ranges,
                    "unresolved": unresolved,
                    "origin": origin,
                },
            }
        )
    return result


def validate_bound(value, source_id, version_id, parsed):
    if not isinstance(value, dict) or value.get("format") != "bound-location-v1":
        return
    if set(value) != {"format", "raw", "ranges", "unresolved", "origin"}:
        raise ValueError("Invalid bound location")
    origin = value["origin"]
    if not isinstance(origin, dict) or set(origin) != {*_IDENTITY, "request", "window", "binding"}:
        raise ValueError("Invalid bound location origin")
    if tuple(origin[key] for key in _IDENTITY) != (source_id, version_id, parsed.id):
        raise ValueError("Bound location source identity mismatch")
    for key in ("request", "window", "binding"):
        valid_id(origin[key])
    if not isinstance(value["ranges"], list) or not isinstance(value["unresolved"], list):
        raise ValueError("Invalid bound location content")
    if not all(isinstance(item, str) for item in value["unresolved"]):
        raise ValueError("Invalid unresolved location")
    for selected in value["ranges"]:
        for order, _, _ in range_intervals(selected, parsed, "bound location"):
            if "attachment" in parsed.blocks[order].location:
                raise ValueError("Bound location references unread attachment")


def read_bound(value, source, parsed, store):
    validate_bound(value, source.source_id, source.id, parsed)
    binding = load_binding(store, value["origin"]["binding"], source, parsed)
    if any(binding[key] != value["origin"][key] for key in (*_IDENTITY, "request", "window")):
        raise ValueError("Bound location request identity mismatch")
    ranges, unresolved = _resolve_aliases(value["raw"], binding, parsed)
    if ranges != value["ranges"] or unresolved != value["unresolved"]:
        raise ValueError("Bound location differs from its original request")
    return deepcopy(ranges), list(unresolved)
