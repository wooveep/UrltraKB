"""Bounded, dependency-preserving projections of the cumulative planning ledger."""

from __future__ import annotations

import re
from typing import Any

_WIKILINK_TARGET = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_PHRASE = re.compile(r"[^\w]+", re.UNICODE)


def _normalized_phrase(value: str) -> str:
    return " ".join(_PHRASE.sub(" ", value.casefold()).split())


def _mentions(text: str, value: Any) -> bool:
    """Recognize a named prior prerequisite in ordinary later prose."""

    if not isinstance(value, str) or not (phrase := _normalized_phrase(value)):
        return False
    return f" {phrase} " in f" {_normalized_phrase(text)} "


def touches_target(ranges: list[Any], start: int, end: int) -> bool:
    """Whether an exact source range intersects the current T block span."""

    for value in ranges:
        if isinstance(value, dict):
            left = value.get("block_index")
            right = left + 1 if type(left) is int else None
        elif isinstance(value, list) and len(value) == 2:
            left, right = value
        else:
            continue
        if type(left) is int and type(right) is int and left < end and right > start:
            return True
    return False


def prompt_condition_template(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use a constant-size parser-condition envelope before W is selected.

    The actual conditions are later intersected with T by
    :func:`project_source_conditions`.  Admission only needs to reserve their
    fixed wire shape; serializing every global diagnostic here would make an
    otherwise small first W fail before that per-target projection happens.
    """
    if any(
        isinstance(item, dict)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("reason"), str)
        for item in conditions
    ):
        return [
            {
                "kind": "parsing_limitation",
                "reason": "Target-specific parser limitations are supplied separately.",
                "ranges": [[0, 1]],
            }
        ]
    return []


def _range_intervals(parsed: Any, ranges: list[Any]) -> list[tuple[int, int, int]]:
    intervals = []
    for value in ranges:
        if isinstance(value, dict):
            index, start, end = (
                value.get("block_index"),
                value.get("start_char"),
                value.get("end_char"),
            )
            if (
                type(index) is int
                and type(start) is int
                and type(end) is int
                and 0 <= index < len(parsed.blocks)
                and 0 <= start < end <= parsed.blocks[index].chars
            ):
                intervals.append((index, start, end))
            continue
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and type(value[0]) is int
            and type(value[1]) is int
            and 0 <= value[0] < value[1] <= len(parsed.blocks)
        ):
            intervals.extend(
                (index, 0, parsed.blocks[index].chars)
                for index in range(value[0], value[1])
                if parsed.blocks[index].chars
            )
    return intervals


def _compact_intervals(parsed: Any, intervals: list[tuple[int, int, int]]) -> list[Any]:
    """Merge exact intervals and represent contiguous complete blocks compactly."""

    merged: list[tuple[int, int, int]] = []
    for index, start, end in sorted(intervals):
        if merged and index == merged[-1][0] and start <= merged[-1][2]:
            merged[-1] = (index, merged[-1][1], max(end, merged[-1][2]))
        else:
            merged.append((index, start, end))
    result: list[Any] = []
    complete_start = complete_end = None

    def flush_complete() -> None:
        nonlocal complete_start, complete_end
        if complete_start is not None:
            result.append([complete_start, complete_end])
        complete_start = complete_end = None

    for index, start, end in merged:
        if start == 0 and end == parsed.blocks[index].chars:
            if complete_end == index:
                complete_end = index + 1
            else:
                flush_complete()
                complete_start, complete_end = index, index + 1
            continue
        flush_complete()
        result.append({"block_index": index, "start_char": start, "end_char": end})
    flush_complete()
    return result


def project_source_conditions(
    conditions: list[dict[str, Any]], parsed: Any, target_ranges: list[Any]
) -> list[dict[str, Any]]:
    """Bind parser facts to the current T without serializing global O(N) gaps."""

    target = _range_intervals(parsed, target_ranges)
    result = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        ranges = _range_intervals(parsed, condition.get("ranges", []))
        overlap = [
            (index, max(start, target_start), min(end, target_end))
            for index, start, end in ranges
            for target_index, target_start, target_end in target
            if index == target_index and max(start, target_start) < min(end, target_end)
        ]
        if not overlap:
            continue
        result.append(
            {
                "kind": condition.get("kind"),
                "reason": condition.get("reason"),
                "ranges": _compact_intervals(parsed, overlap),
            }
        )
    return result
