"""Effective readable-text coverage of a finished document-planning attempt."""

from __future__ import annotations

from math import isclose
from typing import Any, cast

from openkb.agent.document_plan import DocumentPlan, range_intervals
from openkb.agent.document_range_validation import merged_intervals
from openkb.source_coverage import parsing_gaps
from openkb.sources import valid_id


def _add(
    target: dict[int, list[tuple[int, int]]], values: list[Any], parsed: Any, label: str
) -> None:
    for value in values:
        for index, start, end in range_intervals(value, parsed, label):
            target.setdefault(index, []).append((start, end))


def _length(rows: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merged_intervals(rows))


def planning_page_scopes(plan: DocumentPlan | None, parsed: Any) -> dict[str, Any]:
    """Keep page-level fallback use visible even when precise ranges cover it."""
    counts = {"section": 0, "explicit_range": 0, "target_fallback": 0}
    readable = {
        index: block.chars
        for index, block in enumerate(parsed.blocks)
        if "attachment" not in block.location
        and block.kind not in {"image", "figure", "attachment"}
        and block.chars
    }
    whole_source = []
    for page in plan.pages if plan else []:
        if page.state != "ready":
            continue
        counts[page.scope_resolution] = counts.get(page.scope_resolution, 0) + 1
        ranges: dict[int, list[tuple[int, int]]] = {}
        _add(ranges, page.subject_ranges, parsed, "page subject")
        if readable and all(
            _length(ranges.get(index, [])) == size for index, size in readable.items()
        ):
            whole_source.append({"name": page.name, "scope_resolution": page.scope_resolution})
    return {"by_resolution": counts, "whole_source_pages": whole_source}


def planning_coverage(
    plan: DocumentPlan | None,
    parsed: Any,
    *,
    omission_count: int | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Count P, S, and M as interval unions; blocked pages contribute no P."""
    if (
        plan is not None
        and plan.metadata.get("protocol") == "document-plan-v3"
        or (plan is None and outcome in {"complete", "partial", "empty"})
    ):
        return _planning_coverage_v3(plan, parsed, outcome=outcome, omission_count=omission_count)
    pages: dict[int, list[tuple[int, int]]] = {}
    source_only: dict[int, list[tuple[int, int]]] = {}
    if plan is not None:
        for page in plan.pages:
            if page.state != "ready":
                continue
            _add(pages, page.subject_ranges, parsed, "executable page subject")
            for context in page.necessary_context:
                _add(pages, context.get("ranges", []), parsed, "executable page context")
        for item in plan.source_only:
            _add(source_only, item.ranges, parsed, "source-only plan route")
    total = page_chars = source_chars = 0
    missing_ranges = []
    for index, block in enumerate(parsed.blocks):
        if "attachment" in block.location or block.kind in {"image", "figure", "attachment"}:
            continue
        chars = block.chars
        if type(chars) is not int or chars < 0:
            raise ValueError("Invalid readable text denominator")
        total += chars
        p = merged_intervals(pages.get(index, []))
        s = merged_intervals(source_only.get(index, []))
        page_chars += _length(p)
        source_chars += _length([*p, *s]) - _length(p)
        covered = merged_intervals([*p, *s])
        cursor = 0
        for start, end in covered:
            if start > cursor:
                missing_ranges.append(
                    {
                        "block_id": block.id,
                        "start": cursor,
                        "end": start,
                    }
                )
            cursor = max(cursor, end)
        if cursor < chars:
            missing_ranges.append({"block_id": block.id, "start": cursor, "end": chars})
    missing = total - page_chars - source_chars
    omissions = plan.planning_omissions if plan is not None else []
    if omission_count is None:
        omission_count = len(omissions)
    if type(omission_count) is not int or omission_count < 0:
        raise ValueError("Invalid planning omission count")
    blocked = sum(page.state == "blocked" for page in plan.pages) if plan else 0
    parser_gap_count = len(parsing_gaps(parsed))
    return {
        "protocol": "document-planning-coverage-v1",
        "status": "partial"
        if missing or omission_count or blocked or parser_gap_count
        else "complete",
        "readable_chars": total,
        "executable_page_chars": page_chars,
        "source_only_chars": source_chars,
        "missing_chars": missing,
        "effective_ratio": (page_chars + source_chars) / total if total else None,
        "page_ratio": page_chars / total if total else None,
        "source_only_ratio": source_chars / total if total else None,
        "missing_ratio": missing / total if total else None,
        "missing_ranges": missing_ranges,
        "blocked_pages": blocked,
        "planning_omissions": omission_count,
        "parser_gaps": parser_gap_count,
    }


def _planning_coverage_v3(
    plan: DocumentPlan | None,
    parsed: Any,
    *,
    outcome: str | None = None,
    omission_count: int | None = None,
) -> dict[str, Any]:
    """Report precise page ranges, conservative fallbacks, and unrouted text."""
    precise: dict[int, list[tuple[int, int]]] = {}
    fallback: dict[int, list[tuple[int, int]]] = {}
    for page in plan.pages if plan is not None else []:
        if page.state != "ready":
            continue
        destination = fallback if page.scope_resolution == "target_fallback" else precise
        _add(destination, page.subject_ranges, parsed, "page subject")
    total = precise_chars = fallback_chars = 0
    missing_ranges = []
    for index, block in enumerate(parsed.blocks):
        if "attachment" in block.location or block.kind in {"image", "figure", "attachment"}:
            continue
        chars = block.chars
        if type(chars) is not int or chars < 0:
            raise ValueError("Invalid readable text denominator")
        total += chars
        p = merged_intervals(precise.get(index, []))
        f = merged_intervals(fallback.get(index, []))
        precise_chars += _length(p)
        fallback_chars += _length([*p, *f]) - _length(p)
        cursor = 0
        for start, end in merged_intervals([*p, *f]):
            if start > cursor:
                missing_ranges.append({"block_id": block.id, "start": cursor, "end": start})
            cursor = max(cursor, end)
        if cursor < chars:
            missing_ranges.append({"block_id": block.id, "start": cursor, "end": chars})
    unrouted = total - precise_chars - fallback_chars
    return {
        "protocol": "document-planning-coverage-v2",
        "status": outcome or (plan.metadata.get("outcome", "complete") if plan else "empty"),
        "readable_chars": total,
        "precise_chars": precise_chars,
        "fallback_chars": fallback_chars,
        "unrouted_chars": unrouted,
        "precise_ratio": precise_chars / total if total else None,
        "fallback_ratio": fallback_chars / total if total else None,
        "unrouted_ratio": unrouted / total if total else None,
        "missing_ranges": missing_ranges,
        "planning_omissions": omission_count
        if omission_count is not None
        else len(plan.planning_omissions)
        if plan
        else 0,
        "parser_gaps": len(parsing_gaps(parsed)),
    }


def validate_planning_coverage(value: dict[str, Any]) -> None:
    """Keep the planning projection separate from publication coverage."""
    if value == {}:
        return
    if isinstance(value, dict) and value.get("protocol") == "document-planning-coverage-v2":
        _validate_planning_coverage_v3(value)
        return
    if not isinstance(value, dict) or value.get("protocol") != "document-planning-coverage-v1":
        raise ValueError("Invalid planning coverage protocol")
    expected = {
        "protocol",
        "status",
        "readable_chars",
        "executable_page_chars",
        "source_only_chars",
        "missing_chars",
        "effective_ratio",
        "page_ratio",
        "source_only_ratio",
        "missing_ratio",
        "missing_ranges",
        "blocked_pages",
        "planning_omissions",
        "parser_gaps",
    }
    if set(value) != expected:
        raise ValueError("Invalid planning coverage fields")
    total = value.get("readable_chars")
    counts = [
        value.get(key) for key in ("executable_page_chars", "source_only_chars", "missing_chars")
    ]
    if type(total) is not int or total < 0 or any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("Invalid planning coverage counts")
    counted = [cast(int, n) for n in counts]
    if counted[0] + counted[1] + counted[2] != total or value.get("status") not in {
        "complete",
        "partial",
    }:
        raise ValueError("Inconsistent planning coverage")
    expected_ratios = {
        "effective_ratio": (counted[0] + counted[1]) / total if total else None,
        "page_ratio": counted[0] / total if total else None,
        "source_only_ratio": counted[1] / total if total else None,
        "missing_ratio": counted[2] / total if total else None,
    }
    for key, expected_ratio in expected_ratios.items():
        ratio = value.get(key)
        if expected_ratio is None and ratio is not None:
            raise ValueError("Zero-denominator planning ratio must be unavailable")
        if expected_ratio is not None and (
            type(ratio) not in {int, float}
            or not isclose(cast(float, ratio), expected_ratio, rel_tol=0, abs_tol=1e-12)
        ):
            raise ValueError("Invalid planning coverage ratio")
    ranges = value.get("missing_ranges")
    if not isinstance(ranges, list):
        raise ValueError("Invalid planning missing ranges")
    by_block: dict[str, list[tuple[int, int]]] = {}
    for row in ranges:
        if not isinstance(row, dict) or set(row) != {"block_id", "start", "end"}:
            raise ValueError("Invalid planning missing range")
        valid_id(row["block_id"])
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid planning missing range bounds")
        by_block.setdefault(row["block_id"], []).append((start, end))
    if sum(end - start for spans in by_block.values() for start, end in spans) != counted[2] or any(
        len(merged_intervals(spans)) != len(spans) for spans in by_block.values()
    ):
        raise ValueError("Inconsistent planning missing ranges")
    for key in ("blocked_pages", "planning_omissions", "parser_gaps"):
        count = value.get(key)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid planning coverage diagnostics")
    partial = any(
        (counted[2], value["blocked_pages"], value["planning_omissions"], value["parser_gaps"])
    )
    if value["status"] != ("partial" if partial else "complete"):
        raise ValueError("Inconsistent planning coverage status")


def _validate_planning_coverage_v3(value: dict[str, Any]) -> None:
    expected = {
        "protocol",
        "status",
        "readable_chars",
        "precise_chars",
        "fallback_chars",
        "unrouted_chars",
        "precise_ratio",
        "fallback_ratio",
        "unrouted_ratio",
        "missing_ranges",
        "planning_omissions",
        "parser_gaps",
    }
    if set(value) != expected or value["status"] not in {"complete", "partial", "empty"}:
        raise ValueError("Invalid v3 planning coverage")
    counts = [value[key] for key in ("precise_chars", "fallback_chars", "unrouted_chars")]
    total = value["readable_chars"]
    if any(type(count) is not int or count < 0 for count in [total, *counts]):
        raise ValueError("Invalid v3 planning coverage counts")
    if sum(counts) != total:
        raise ValueError("Inconsistent v3 planning coverage counts")
    for key, count in zip(("precise_ratio", "fallback_ratio", "unrouted_ratio"), counts):
        expected_ratio = count / total if total else None
        actual = value[key]
        if (
            expected_ratio is None
            and actual is not None
            or expected_ratio is not None
            and (
                type(actual) not in {int, float}
                or not isclose(actual, expected_ratio, rel_tol=0, abs_tol=1e-12)
            )
        ):
            raise ValueError("Invalid v3 planning coverage ratio")
    if not isinstance(value["missing_ranges"], list):
        raise ValueError("Invalid v3 planning missing ranges")
    by_block: dict[str, list[tuple[int, int]]] = {}
    for row in value["missing_ranges"]:
        if not isinstance(row, dict) or set(row) != {"block_id", "start", "end"}:
            raise ValueError("Invalid v3 planning missing range")
        valid_id(row["block_id"])
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("Invalid v3 planning missing range bounds")
        by_block.setdefault(row["block_id"], []).append((start, end))
    if sum(end - start for spans in by_block.values() for start, end in spans) != counts[2] or any(
        len(merged_intervals(spans)) != len(spans) for spans in by_block.values()
    ):
        raise ValueError("Inconsistent v3 planning missing ranges")
    for key in ("planning_omissions", "parser_gaps"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("Invalid v3 planning diagnostics")
