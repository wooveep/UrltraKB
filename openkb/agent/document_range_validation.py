"""Exact source-range validation for the DocumentPlan protocol."""

from __future__ import annotations

from typing import Any

from openkb.agent.document_plan_issues import PlanValidationError, ValidationIssue, reject


def require_nonempty_ranges(
    ranges: list[Any], path: str, label: str, *, message: str | None = None
) -> None:
    if not ranges:
        reject(
            message or f"{label} needs exact source ranges",
            code="range_empty",
            path=path,
            category="evidence",
            expected="one or more authorized half-open ranges",
            actual=ranges,
            allowed_action="reselect_evidence",
        )


def validate_ranges(
    ranges: Any,
    total_blocks: int,
    context_label: str,
    *,
    block_chars: list[int] | None = None,
    ignored_blocks: set[int] | None = None,
    field_path: str | None = None,
) -> None:
    """Validate block or character ranges before testing their authorization."""
    path = field_path or context_label
    if not isinstance(ranges, list):
        reject(
            f"Ranges in {context_label} must be a list",
            code="invalid_range_shape",
            path=path,
            category="shape",
            expected="list of half-open ranges",
            actual=ranges,
        )
    for position, value in enumerate(ranges):
        item_path = f"{path}[{position}]"
        if isinstance(value, dict):
            if set(value) != {"block_index", "start_char", "end_char"} or any(
                type(value[field]) is not int for field in value
            ):
                reject(
                    f"Invalid character range in {context_label}: {value}",
                    code="invalid_range_shape",
                    path=item_path,
                    category="shape",
                    expected="block_index/start_char/end_char integer fields",
                    actual=value,
                )
            index, start, end = value["block_index"], value["start_char"], value["end_char"]
            if index < 0 or index >= total_blocks or start < 0 or end <= start:
                reject(
                    f"Character range out of bounds in {context_label}: {value}",
                    code="range_empty" if end <= start else "range_out_of_bounds",
                    path=item_path,
                    category="evidence",
                    expected={"total_blocks": total_blocks, "half_open": True},
                    actual=value,
                    allowed_action="reselect_evidence",
                )
            if block_chars is not None and (
                len(block_chars) != total_blocks or end > block_chars[index]
            ):
                reject(
                    f"Character range out of bounds in {context_label}: {value}",
                    code="range_out_of_bounds",
                    path=item_path,
                    category="evidence",
                    expected={
                        "max_chars": block_chars[index] if len(block_chars) > index else None
                    },
                    actual=value,
                    allowed_action="reselect_evidence",
                )
            if index in (ignored_blocks or set()):
                reject(
                    f"Range in {context_label} references unread attachment content",
                    code="unread_attachment_range",
                    path=item_path,
                    category="evidence",
                    expected="supplied readable source evidence",
                    actual=value,
                    allowed_action="reselect_evidence",
                )
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or type(value[0]) is not int
            or type(value[1]) is not int
        ):
            reject(
                f"Invalid range in {context_label}: {value}",
                code="invalid_range_shape",
                path=item_path,
                category="shape",
                expected="[start,end) integers",
                actual=value,
            )
        start, end = value
        if start < 0 or end <= start or end > total_blocks:
            reject(
                f"Range out of bounds in {context_label}: [{start}, {end}] (total {total_blocks})",
                code="range_empty" if end <= start else "range_out_of_bounds",
                path=item_path,
                category="evidence",
                expected={"total_blocks": total_blocks, "half_open": True},
                actual=value,
                allowed_action="reselect_evidence",
            )
        if any(index in (ignored_blocks or set()) for index in range(start, end)):
            reject(
                f"Range in {context_label} references unread attachment content",
                code="unread_attachment_range",
                path=item_path,
                category="evidence",
                expected="supplied readable source evidence",
                actual=value,
                allowed_action="reselect_evidence",
            )


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
    *,
    field_path: str | None = None,
    item_ref: str = "$",
) -> None:
    for position, value in enumerate(ranges):
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not interval_is_covered(index, start, end, authorized):
                reject(
                    f"Range in {context_label} must stay within the current target's exact ranges",
                    code="target_range_violation",
                    path=(f"{field_path}[{position}]" if field_path is not None else context_label),
                    category="evidence",
                    expected="range within the current target's exact intervals",
                    actual=value,
                    source_ranges=[value],
                    allowed_action="reselect_evidence",
                    item_ref=item_ref,
                    allowed_operations=("replace_range",),
                )


def validate_evidence_ranges(
    ranges: list[Any],
    evidence_intervals: dict[int, list[tuple[int, int]]],
    context_label: str,
    block_chars: list[int] | None,
    *,
    field_path: str | None = None,
    item_ref: str = "$",
) -> None:
    for position, value in enumerate(ranges):
        for index, start, end in range_intervals(value, block_chars=block_chars):
            if not interval_is_covered(index, start, end, evidence_intervals):
                reject(
                    f"Range in {context_label} must stay within supplied frozen evidence",
                    code="evidence_range_violation",
                    path=(f"{field_path}[{position}]" if field_path is not None else context_label),
                    category="evidence",
                    expected="range within supplied frozen evidence",
                    actual=value,
                    source_ranges=[value],
                    allowed_action="reselect_evidence",
                    item_ref=item_ref,
                    allowed_operations=("replace_range",),
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


def subtract_exact_ranges(
    original: list[Any], removed: list[Any], block_chars: list[int]
) -> list[Any]:
    """Subtract validated exact intervals, preserving untouched character spans."""
    cuts: dict[int, list[tuple[int, int]]] = {}
    for value in removed:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            cuts.setdefault(index, []).append((start, end))
    cuts = {index: merged_intervals(rows) for index, rows in cuts.items()}
    kept: list[tuple[int, int, int]] = []
    for value in original:
        for index, start, end in range_intervals(value, block_chars=block_chars):
            cursor = start
            for left, right in cuts.get(index, []):
                if right <= cursor or left >= end:
                    continue
                if cursor < left:
                    kept.append((index, cursor, min(left, end)))
                cursor = max(cursor, right)
                if cursor >= end:
                    break
            if cursor < end:
                kept.append((index, cursor, end))
    result: list[Any] = []
    for index, start, end in kept:
        if start == 0 and end == block_chars[index]:
            if result and isinstance(result[-1], list) and result[-1][1] == index:
                result[-1][1] = index + 1
            else:
                result.append([index, index + 1])
        else:
            result.append({"block_index": index, "start_char": start, "end_char": end})
    return result


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
    issues = target_coverage_issues(
        target_intervals,
        pages,
        source_only,
        unresolved,
        block_chars=block_chars,
        ignored_blocks=ignored_blocks,
    )
    if issues:
        raise PlanValidationError("Current target has conflicting or unassigned source", issues)


def target_coverage_issues(
    target_intervals: dict[int, list[tuple[int, int]]],
    pages: list[dict[str, Any]],
    source_only: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    *,
    block_chars: list[int] | None,
    ignored_blocks: set[int],
) -> list[ValidationIssue]:
    """Collect every independent conflict and exact uncovered interval."""
    subjects: dict[int, list[tuple[int, int, int]]] = {}
    for page_index, page in enumerate(pages):
        for value in page["subject_ranges"]:
            for index, start, end in range_intervals(value, block_chars=block_chars):
                subjects.setdefault(index, []).append((start, end, page_index))
    conflicts: list[ValidationIssue] = []
    for source_index, item in enumerate(source_only):
        for range_index, value in enumerate(item["ranges"]):
            overlapping: set[int] = set()
            exact_conflict: list[Any] = []
            for index, start, end in range_intervals(value, block_chars=block_chars):
                overlapping_here = {
                    page_index
                    for left, right, page_index in subjects.get(index, [])
                    if start < right and left < end
                }
                overlapping.update(overlapping_here)
                exact_conflict.extend(
                    {
                        "block_index": index,
                        "start_char": max(start, left),
                        "end_char": min(end, right),
                    }
                    for left, right, _ in subjects.get(index, [])
                    if start < right and left < end
                )
            if overlapping:
                conflicts.append(
                    ValidationIssue(
                        code="source_only_conflict",
                        path=f"source_only[{source_index}].ranges[{range_index}]",
                        category="semantic",
                        expected="source-only range outside every page subject",
                        actual=value,
                        source_ranges=exact_conflict,
                        related_paths=[
                            f"page_changes[{page_index}].subject_ranges"
                            for page_index in sorted(overlapping)
                        ],
                        allowed_action="reselect_evidence",
                    )
                )
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

    gaps: list[tuple[int, int, int]] = []
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
                    gaps.append((index, cursor, min(left, end)))
                cursor = max(cursor, right)
                if cursor >= end:
                    break
            if cursor < end:
                gaps.append((index, cursor, end))
    # Whole adjacent blocks are one diagnostic. A partial character gap remains exact.
    grouped: list[list[Any]] = []
    for index, start, end in gaps:
        whole = block_chars is not None and start == 0 and end == block_chars[index]
        if whole and grouped and grouped[-1][2] and grouped[-1][1] == index:
            grouped[-1][1] = index + 1
        else:
            grouped.append([index, index + 1, whole, start, end])
    for first, last, whole, start, end in grouped:
        source_range: Any = (
            [first, last] if whole else {"block_index": first, "start_char": start, "end_char": end}
        )
        conflicts.append(
            ValidationIssue(
                code="coverage_gap",
                path=f"coverage[{first}]",
                category="coverage",
                expected="one authorized content route",
                actual=None,
                source_ranges=[source_range],
                item_ref="plan",
                allowed_action="reselect_evidence",
                allowed_operations=("replace_field", "append_item"),
            )
        )
    return conflicts
