"""Prepare one tolerant suggestion using full-source navigation and real evidence reads."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from openkb.agent.document_page_evidence import page_evidence
from openkb.agent.document_plan import PagePlan, RangeValue, range_intervals
from openkb.agent.document_planning_admission import validate_navigation
from openkb.agent.document_planning_locations import context_choices, resolve_hint
from openkb.agent.document_planning_pages import normalized_name
from openkb.evidence_search import literal_blocks
from openkb.implementation import module_revision
from openkb.sources import content_id


def preparation_rules() -> str:
    return content_id(
        {
            name: module_revision(name)
            for name in (
                __name__,
                "openkb.evidence_search",
                "openkb.agent.document_planning_locations",
                "openkb.agent.document_page_evidence",
            )
        }
    )


@dataclass(frozen=True)
class PreparedPage:
    page: PagePlan
    evidence: dict[str, Any] | None = None
    occurrences: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


def _navigation(navigation: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    paths: dict[str, list[str]] = {}
    for index, node in enumerate((navigation or {}).get("nodes", [])):
        identity = node.get("id", str(index))
        path = [*paths.get(node.get("parent"), []), node["title"]]
        paths[identity] = path
        rows.append(
            {
                "section_key": f"section:{identity}",
                "title": node["title"],
                "heading_path": path,
                "original_range": [node["start"], node["end"]],
            }
        )
    return rows


def _headings(block: Any) -> list[str]:
    path = block.location.get("headings", block.location.get("heading_path", []))
    return path if isinstance(path, list) and all(isinstance(v, str) for v in path) else []


def _original_section(index: int, parsed: Any) -> list[RangeValue]:
    """Read the complete local heading context, or a bounded paragraph neighborhood."""
    path = _headings(parsed.blocks[index])
    start, end = index, index + 1
    if path:
        while start and _headings(parsed.blocks[start - 1])[: len(path)] == path:
            start -= 1
        while end < len(parsed.blocks) and _headings(parsed.blocks[end])[: len(path)] == path:
            end += 1
    elif parsed.blocks[index].kind == "heading":
        while end < len(parsed.blocks) and parsed.blocks[end].kind != "heading":
            end += 1
    else:
        start, end = max(0, index - 1), min(len(parsed.blocks), index + 2)
    return [[start, end]]


def _resolve(
    value: Any,
    rows: list[dict[str, Any]],
    source: Any,
    parsed: Any,
    reader: Any,
    max_chars: int,
    notes: list[str],
    description: str,
) -> tuple[list[RangeValue], str]:
    try:
        return resolve_hint(
            value,
            rows,
            parsed,
            notes=notes,
            evidence={
                "blocks": [
                    {"id": block.id, "order": index} for index, block in enumerate(parsed.blocks)
                ]
            },
        )
    except ValueError:
        # An unknown explicit key cannot be silently corrected by title search.
        if isinstance(value, dict) and set(value) == {"heading_path"}:
            value = value["heading_path"]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = " > ".join(value)
        if not isinstance(value, str) or "section:" in value:
            return [], "unresolved_location"
    clue = value.strip(" `#")
    if not clue or len(clue) > 512:
        return [], "unresolved_location"
    matches = [
        index
        for index, block in enumerate(parsed.blocks)
        if _headings(block) and normalized_name(_headings(block)[-1]) == normalized_name(clue)
    ]
    if not matches:
        path = [normalized_name(part) for part in re.split(r"\s*(?:>|→| / )\s*", clue)]
        matches = [
            index
            for index, block in enumerate(parsed.blocks)
            if [normalized_name(part) for part in _headings(block)[-len(path) :]] == path
        ]
    if not matches:
        found = literal_blocks(reader, source, parsed, clue, max_chars=max_chars * 32)
        if found is None:
            return [], "source_search_budget_limited"
        matches = found
    candidates = []
    for index in matches:
        candidate = _original_section(index, parsed)
        if candidate not in candidates:
            candidates.append(candidate)
    if len(candidates) != 1:
        narrowed = [
            candidate
            for candidate in candidates
            if any(
                re.search(
                    r"(?<!\w)" + re.escape(normalized_name(part)) + r"(?!\w)",
                    normalized_name(description),
                )
                for part in _headings(parsed.blocks[cast(list[int], candidate[0])[0]])[:-1]
            )
        ]
        if len(narrowed) != 1:
            return [], "ambiguous_location" if candidates else "unresolved_location"
        candidates = narrowed
    note = "已读取原文定位线索：" + clue
    if note not in notes:
        notes.append(note)
    return candidates[0], "section"


def prepare_page(
    page: PagePlan,
    source: Any,
    parsed: Any,
    navigation: Any,
    reader: Any,
    *,
    max_chars: int = 64000,
    retry_skipped: bool = False,
) -> PreparedPage:
    """Return a read-backed page or a local skip; storage/identity errors propagate."""
    validate_navigation(navigation, source, parsed)
    result = deepcopy(page)
    if result.state == "skipped" and not retry_skipped:
        return PreparedPage(result, reason="previously_skipped")
    rows = _navigation(navigation)
    # Saved ranges are program data. Corruption here is not a bad model hint.
    for value in [*result.subject_ranges, *result.context_ranges]:
        range_intervals(value, parsed, "saved page evidence")

    def skip(reason: str) -> PreparedPage:
        result.subject_ranges = deepcopy(page.subject_ranges)
        result.context_ranges = deepcopy(page.context_ranges)
        result.scope_resolution = page.scope_resolution
        result.state, result.quality, result.review_receipt = "skipped", "planned", None
        note = "取证跳过：" + reason
        if note not in result.planning_notes:
            result.planning_notes.append(note)
        return PreparedPage(result, reason=reason)

    subject_hints = [row for row in result.location_hints if row["role"] == "subject"]
    if subject_hints or not result.subject_ranges:
        clues = subject_hints or [row for row in result.location_hints if row["role"] == "related"]
        clues = clues or [{"value": result.title}]
        for hint in clues:
            for clue in context_choices(hint["value"], rows):
                ranges, scope = _resolve(
                    clue,
                    rows,
                    source,
                    parsed,
                    reader,
                    max_chars,
                    result.planning_notes,
                    result.title + " " + result.purpose,
                )
                if not ranges:
                    return skip(scope)
                result.subject_ranges.extend(r for r in ranges if r not in result.subject_ranges)
                result.scope_resolution = scope
    for hint in result.location_hints:
        if hint["role"] != "context":
            continue
        for clue in context_choices(hint["value"], rows):
            ranges, reason = _resolve(
                clue,
                rows,
                source,
                parsed,
                reader,
                max_chars,
                result.planning_notes,
                result.title + " " + result.purpose,
            )
            if not ranges:
                return skip("context_" + reason)
            result.context_ranges.extend(
                r
                for r in ranges
                if r not in result.context_ranges and r not in result.subject_ranges
            )
    if not result.subject_ranges:
        return skip("no_subject_evidence")
    size = sum(
        end - start
        for value in [*result.subject_ranges, *result.context_ranges]
        for _, start, end in range_intervals(value, parsed, "prepared page")
    )
    if size > max_chars:
        return skip("page_evidence_budget_limited")
    evidence, occurrences = page_evidence(result, reader, source, parsed, strict=True)
    if not any(
        any(route["route"] == "page_body" for route in row["routes"]) for row in occurrences
    ):
        return skip("no_readable_subject_evidence")
    result.state = "ready"
    return PreparedPage(result, evidence, tuple(occurrences))
