"""Resolve supplied source locations without guessing unavailable evidence."""

from __future__ import annotations

import re
from typing import Any

from openkb.agent.document_plan import RangeValue, range_intervals


def _location_choices(clue: str) -> list[str]:
    """Split selections without interpreting punctuation inside path annotations."""
    parts = []
    start = depth = 0
    for index, char in enumerate(clue):
        if char in "(（":
            depth += 1
        elif char in ")）":
            depth = max(0, depth - 1)
        elif char in ";；\n" and depth == 0:
            parts.append(clue[start:index].strip())
            start = index + 1
    parts.append(clue[start:].strip())
    return [part for part in parts if part]


def resolve_location(
    value: Any,
    navigation: list[dict[str, Any]],
    target: list[RangeValue],
    parsed: Any,
    evidence: dict[str, Any] | None = None,
) -> tuple[list[RangeValue], str]:
    if value is None or value == "":
        if not target:
            raise ValueError("missing_location")
        return target, "target_fallback"
    if isinstance(value, list) and all(isinstance(item, (list, dict)) for item in value):
        supplied = {
            block["id"]: block["order"]
            for block in (evidence or {}).get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("id"), str)
        }
        resolved: list[RangeValue] = []
        for item in value:
            if isinstance(item, dict) and "section_key" in item:
                selected_context, _ = resolve_location(
                    item["section_key"], navigation, target, parsed, evidence
                )
                resolved.extend(selected_context)
                continue
            if isinstance(item, dict) and {"from_block", "through_block"} <= set(item):
                first = supplied.get(item["from_block"])
                last = supplied.get(item["through_block"])
                if first is None or last is None or first > last:
                    raise ValueError("unknown_location")
                item = [first, last + 1]
            elif isinstance(item, dict) and "block" in item:
                index = supplied.get(item["block"])
                if index is None:
                    raise ValueError("unknown_location")
                item = {
                    "block_index": index,
                    "start_char": item.get("start_char"),
                    "end_char": item.get("end_char"),
                }
            range_intervals(item, parsed, "planned page")
            resolved.append(item)
        return resolved, "explicit_range"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if any(not item.strip() for item in value):
            raise ValueError("unknown_location")
        # A path array can omit ancestors under the same exact suffix rule.
        if any(node.get("heading_path", [])[-len(value) :] == value for node in navigation):
            value = " / ".join(value)
        else:
            combined: list[RangeValue] = []
            for choice in value:
                located, _ = resolve_location(choice, navigation, target, parsed, evidence)
                combined.extend(item for item in located if item not in combined)
            return combined, "section"
    clue = str(value).strip(" `")
    clue = re.sub(r"^(?:heading[ _]path|section[ _]key|章节路径)\s*[：:]\s*", "", clue, flags=re.I)
    parts = _location_choices(clue)
    if len(parts) > 1:
        combined = []
        for part in parts:
            located, _ = resolve_location(part, navigation, target, parsed, evidence)
            combined.extend(item for item in located if item not in combined)
        return combined, "section"
    section_keys = re.findall(r"section:[A-Za-z0-9_-]+", clue)
    if section_keys:
        selected: list[RangeValue] = []
        for section_key in dict.fromkeys(section_keys):
            matches = [node for node in navigation if node.get("section_key") == section_key]
            if len(matches) != 1:
                raise ValueError("unknown_location")
            ranges = matches[0].get("original_ranges")
            if ranges is None and isinstance(matches[0].get("original_range"), list):
                ranges = [matches[0]["original_range"]]
            if not isinstance(ranges, list) or not ranges:
                raise ValueError("unknown_location")
            selected.extend(ranges)
        for selected_range in selected:
            range_intervals(selected_range, parsed, "planned section")
        return selected, "section"
    labelled_path = re.search(
        r"[（(]\s*(?:heading[ _]path|章节路径|标题路径)\s*[：:]\s*([^）)]+)[）)]\s*$",
        clue,
        re.I,
    )
    if labelled_path:
        clue = labelled_path.group(1).strip(" `")
    if clue.isdigit():
        raise ValueError("ambiguous_numeric_location")
    candidates = []
    for node in navigation:
        path = node.get("heading_path", [])
        labels = {node.get("section_key"), " / ".join(path), " > ".join(path)}
        if path:
            labels.add(path[-1])
        if clue in labels:
            candidates.append(node)
    if not candidates:
        # An exact suffix can omit ancestor headings, but must identify one node.
        path_parts = re.split(r"\s*>\s*|\s+/\s+", clue)
        if len(path_parts) > 1:
            candidates = [
                node
                for node in navigation
                if node.get("heading_path", [])[-len(path_parts) :] == path_parts
            ]
    if len(candidates) != 1:
        if candidates:
            raise ValueError("ambiguous_location")
        if " / " in clue:
            parts = [part.strip() for part in clue.split(" / ") if part.strip()]
            combined = []
            if len(parts) > 1:
                for part in parts:
                    located, _ = resolve_location(part, navigation, target, parsed, evidence)
                    combined.extend(value for value in located if value not in combined)
                return combined, "section"
        raise ValueError("unknown_location")
    node = candidates[0]
    originals = node.get("original_ranges")
    if originals is None and isinstance(node.get("original_range"), list):
        originals = [node["original_range"]]
    if not isinstance(originals, list) or not originals:
        raise ValueError("unknown_location")
    for original in originals:
        range_intervals(original, parsed, "planned section")
    return originals, "section"


def within_target(ranges: list[RangeValue], target: list[RangeValue], parsed: Any) -> bool:
    if not ranges:
        return False
    allowed = [
        interval for value in target for interval in range_intervals(value, parsed, "target")
    ]
    return all(
        any(
            index == known_index and left <= start and end <= right
            for known_index, left, right in allowed
        )
        for value in ranges
        for index, start, end in range_intervals(value, parsed, "planned page")
    )
