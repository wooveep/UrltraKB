"""Lossless shape corrections for the v5 planning response."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from openkb.agent.document_plan_annotations import quote_from_evidence
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_range_validation import interval_is_covered, range_intervals


def overview_limitation_reasons(candidate: Any, protocol: str) -> tuple[Any, list[dict[str, str]]]:
    """Keep reasons when a model supplied unused anchors around overview limits."""
    if protocol != "document-plan-v5" or not isinstance(candidate, dict):
        return candidate, []
    overview = candidate.get("overview")
    limitations = overview.get("limitations") if isinstance(overview, dict) else None
    if not isinstance(limitations, list) or not limitations or not all(
        isinstance(row, dict)
        and set(row) == {"ranges", "reason"}
        and isinstance(row["ranges"], list)
        and isinstance(row["reason"], str)
        and row["reason"].strip()
        for row in limitations
    ):
        return candidate, []
    normalized = deepcopy(candidate)
    normalized["overview"]["limitations"] = [row["reason"] for row in limitations]
    return normalized, [{"code": "overview_limitation_reasons", "path": "overview.limitations"}]


def reclassify_literal_external_material(
    candidate: Any, context: Any
) -> tuple[Any, list[dict[str, str]]]:
    """Convert only a uniquely anchored, single-page external document omission."""
    if context.selection_protocol != "document-plan-v5" or not isinstance(candidate, dict):
        return candidate, []
    unresolved = candidate.get("unresolved")
    pages = candidate.get("page_changes")
    if not isinstance(unresolved, list) or not isinstance(pages, list):
        return candidate, []
    resolver = SelectionResolver.from_context(context)
    chars = [block.chars for block in context.parsed.blocks]
    normalized = deepcopy(candidate)
    records: list[dict[str, str]] = []
    for index in range(len(unresolved) - 1, -1, -1):
        row = unresolved[index]
        if not isinstance(row, dict) or row.get("problem_type") != "missing_external_material":
            continue
        target, affected, reason = (
            row.get("missing_target"), row.get("affected_pages"), row.get("reason")
        )
        if (
            not isinstance(target, str) or not target.startswith("《")
            or not target.endswith("》") or not isinstance(affected, list)
            or len(affected) != 1 or not isinstance(affected[0], str)
            or not isinstance(reason, str) or not reason.strip()
        ):
            continue
        matching = [
            page for page in normalized["page_changes"]
            if isinstance(page, dict) and page.get("local_key") == affected[0]
        ]
        if len(matching) != 1:
            continue
        raw_location = row.get("location")
        location = [raw_location] if isinstance(raw_location, dict) else raw_location
        try:
            selected = resolver.decode_ranges(location, "external location", target_only=True)
            subjects = resolver.decode_ranges(
                matching[0]["subject_ranges"], "external page", target_only=True
            )
            quote = quote_from_evidence(selected, context.evidence, chars)
            supplied: dict[int, list[tuple[int, int]]] = {}
            for value in subjects:
                for block, start, end in range_intervals(value, block_chars=chars):
                    supplied.setdefault(block, []).append((start, end))
            contained = all(
                interval_is_covered(block, start, end, supplied)
                for value in selected
                for block, start, end in range_intervals(value, block_chars=chars)
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not contained or quote.casefold().count(target.casefold()) != 1:
            continue
        references = normalized.setdefault("external_references", [])
        if not any(
            isinstance(item, dict)
            and item.get("location") == location
            and item.get("target_document") == target
            for item in references
        ):
            references.append({
                "location": location, "target_document": target, "target_section": None,
                "affected_pages": affected,
            })
        matching[0].setdefault("limitations", []).append({
            "ranges": location, "reason": reason,
        })
        normalized["unresolved"].pop(index)
        records.append({
            "code": "external_reference_reclassified", "path": f"unresolved[{index}]"
        })
    return normalized, records
