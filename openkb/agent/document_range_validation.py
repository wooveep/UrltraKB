"""Exact source-range validation for the DocumentPlan protocol."""

from __future__ import annotations

from typing import Any


def validate_ranges(
    ranges: Any,
    total_blocks: int,
    context_label: str,
    *,
    block_chars: list[int] | None = None,
    ignored_blocks: set[int] | None = None,
) -> None:
    """Validate block or character ranges before testing their authorization."""
    if not isinstance(ranges, list):
        raise ValueError(f"Ranges in {context_label} must be a list")
    for value in ranges:
        if isinstance(value, dict):
            if set(value) != {"block_index", "start_char", "end_char"} or any(
                type(value[field]) is not int for field in value
            ):
                raise ValueError(f"Invalid character range in {context_label}: {value}")
            index, start, end = value["block_index"], value["start_char"], value["end_char"]
            if index < 0 or index >= total_blocks or start < 0 or end <= start:
                raise ValueError(f"Character range out of bounds in {context_label}: {value}")
            if block_chars is not None and (
                len(block_chars) != total_blocks or end > block_chars[index]
            ):
                raise ValueError(f"Character range out of bounds in {context_label}: {value}")
            if index in (ignored_blocks or set()):
                raise ValueError(f"Range in {context_label} references unread attachment content")
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or type(value[0]) is not int
            or type(value[1]) is not int
        ):
            raise ValueError(f"Invalid range in {context_label}: {value}")
        start, end = value
        if start < 0 or end <= start or end > total_blocks:
            raise ValueError(
                f"Range out of bounds in {context_label}: [{start}, {end}] (total {total_blocks})"
            )
        if any(index in (ignored_blocks or set()) for index in range(start, end)):
            raise ValueError(f"Range in {context_label} references unread attachment content")


def target_intervals(
    target_start: int,
    target_end: int,
    *,
    target_ranges: list[Any] | None,
    block_chars: list[int] | None,
) -> dict[int, list[tuple[int, int]]]:
    """Return the exact source intervals authorized for one planning target."""
    if target_ranges is None:
        target_ranges = [[target_start, target_end]]
    validate_ranges(
        target_ranges,
        len(block_chars) if block_chars is not None else target_end,
        "planning target",
        block_chars=block_chars,
    )
    intervals: dict[int, list[tuple[int, int]]] = {}
    for value in target_ranges:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not target_start <= index < target_end:
                raise ValueError("Planning target range is outside its block window")
            intervals.setdefault(index, []).append((start, end))
    return {index: merged_intervals(rows) for index, rows in intervals.items()}


def frozen_evidence_intervals(
    ranges: list[Any], total_blocks: int, block_chars: list[int] | None
) -> dict[int, list[tuple[int, int]]]:
    """Return original intervals that were actually present in frozen evidence."""
    validate_ranges(ranges, total_blocks, "frozen evidence", block_chars=block_chars)
    intervals: dict[int, list[tuple[int, int]]] = {}
    for value in ranges:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            intervals.setdefault(index, []).append((start, end))
    return {index: merged_intervals(rows) for index, rows in intervals.items()}


def merged_intervals(rows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(rows):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def interval_is_covered(
    index: int, start: int, end: int, allowed: dict[int, list[tuple[int, int]]]
) -> bool:
    return any(left <= start and end <= right for left, right in allowed.get(index, []))


def validate_target_ranges(
    ranges: list[Any],
    authorized: dict[int, list[tuple[int, int]]],
    context_label: str,
    block_chars: list[int] | None,
) -> None:
    for value in ranges:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not interval_is_covered(index, start, end, authorized):
                raise ValueError(
                    f"Range in {context_label} must stay within the current target's exact ranges"
                )


def validate_evidence_ranges(
    ranges: list[Any],
    evidence_intervals: dict[int, list[tuple[int, int]]],
    context_label: str,
    block_chars: list[int] | None,
) -> None:
    for value in ranges:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not interval_is_covered(index, start, end, evidence_intervals):
                raise ValueError(
                    f"Range in {context_label} must stay within supplied frozen evidence"
                )


def validate_overview_ranges(
    ranges: list[Any],
    target_end: int,
    context_label: str,
    *,
    target_intervals: dict[int, list[tuple[int, int]]],
    evidence_intervals: dict[int, list[tuple[int, int]]] | None,
    prior_ranges: list[Any],
    total_blocks: int,
    block_chars: list[int] | None,
) -> None:
    """Allow accepted history plus only the current accepted planning target."""
    prior_intervals = frozen_evidence_intervals(prior_ranges, total_blocks, block_chars)
    # Frozen evidence can overlap future movable targets, but cannot make an
    # overview claim accepted before the corresponding target is processed.
    for value in ranges:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not interval_is_covered(
                index, start, end, prior_intervals
            ) and not interval_is_covered(index, start, end, target_intervals):
                raise ValueError(
                    f"Range in {context_label} cannot claim an unread future target "
                    f"before {target_end}"
                )


def range_intervals(value: Any, *, block_chars: list[int] | None) -> list[tuple[int, int, int]]:
    if isinstance(value, dict):
        return [(value["block_index"], value["start_char"], value["end_char"])]
    start, end = value
    if block_chars is None:
        # Direct callers without parse lengths need one symbolic interval per
        # block to detect target omissions after structural validation.
        return [(index, 0, 1) for index in range(start, end)]
    return [(index, 0, block_chars[index]) for index in range(start, end) if block_chars[index]]


def validate_target_coverage(
    target_intervals: dict[int, list[tuple[int, int]]],
    pages: list[dict[str, Any]],
    source_only: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    *,
    block_chars: list[int] | None,
    ignored_blocks: set[int],
) -> None:
    """Reject a target response that silently leaves source responsibility behind."""
    covered: dict[int, list[tuple[int, int]]] = {}

    def add(values: list[Any]) -> None:
        for value in values:
            for index, start, end in range_intervals(value, block_chars=block_chars):
                if index in target_intervals:
                    covered.setdefault(index, []).append((start, end))

    for page in pages:
        add(page["subject_ranges"])
        for context in page["necessary_context"]:
            add(context.get("ranges", []))
            add(context.get("basis_ranges", []))
    for item in source_only:
        add(item["ranges"])
    for item in unresolved:
        add(item["location"])

    for index, required in target_intervals.items():
        if index in ignored_blocks:
            continue
        actual = merged_intervals(covered.get(index, []))
        for start, end in required:
            cursor = start
            for left, right in actual:
                if right <= cursor:
                    continue
                if left > cursor:
                    break
                cursor = max(cursor, right)
                if cursor >= end:
                    break
            if cursor < end:
                raise ValueError(f"Current target leaves source range {index} unaccounted")
