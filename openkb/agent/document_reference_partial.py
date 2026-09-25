"""Retain independent pages when a bounded reference check remains unresolved."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from openkb.agent.document_plan_compiler import PlanningContext, compile_plan_candidate
from openkb.agent.document_plan_salvage import SalvagedPlan
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_reference_check import (
    ReferenceCandidate,
    apply_reference_decisions,
)
from openkb.agent.document_window_receipts import target_ranges
from openkb.sources import content_id


def retain_valid_reference_pages(
    candidate: dict[str, Any],
    context: PlanningContext,
    window: dict[str, Any],
    *,
    valid: dict[tuple[str, str], dict[str, Any]],
    pairs: list[tuple[str, str]],
    references: list[ReferenceCandidate],
    allow_empty_overview: bool,
) -> SalvagedPlan | None:
    """Exclude only current pages whose relation decision never validated."""
    missing = set(pairs) - set(valid)
    if not missing:
        return None
    failed_keys = {page for _, page in missing}
    pages = candidate.get("page_changes")
    if not isinstance(pages, list) or not failed_keys:
        return None
    dropped = [
        page for page in pages
        if isinstance(page, dict) and page.get("local_key") in failed_keys
    ]
    if {page.get("local_key") for page in dropped} != failed_keys:
        return None
    retained = deepcopy(candidate)
    retained["page_changes"] = [
        page for page in retained["page_changes"]
        if page.get("local_key") not in failed_keys
    ]
    if not any(retained.get(name) for name in ("page_changes", "source_only", "unresolved")):
        return None
    for row in retained.get("unresolved", []):
        if isinstance(row, dict) and isinstance(row.get("affected_pages"), list):
            row["affected_pages"] = [
                key for key in row["affected_pages"] if key not in failed_keys
            ]
    retained["unresolved"] = [
        row for row in retained.get("unresolved", []) if row.get("affected_pages")
    ]
    for row in retained.get("external_references", []):
        if isinstance(row, dict) and isinstance(row.get("affected_pages"), list):
            row["affected_pages"] = [
                key for key in row["affected_pages"] if key not in failed_keys
            ]
    retained["external_references"] = [
        row for row in retained.get("external_references", [])
        if row.get("affected_pages")
    ]
    resolver = SelectionResolver.from_context(context)
    omissions: list[Any] = []
    try:
        for page in dropped:
            omissions.extend(resolver.decode_ranges(
                page["subject_ranges"], "failed reference page", target_only=True
            ))
    except (KeyError, TypeError, ValueError):
        omissions = target_ranges({
            "target_ranges": context.target_ranges,
            "target_start": context.target_start,
            "target_end": context.target_end,
        })
    decisions = {pair: row for pair, row in valid.items() if pair[1] not in failed_keys}
    active_references = [
        row for row in references
        if any(pair[0] == row.reference_key for pair in decisions)
    ]
    try:
        retained = apply_reference_decisions(
            retained, decisions, active_references, resolver=resolver
        )
    except (KeyError, TypeError, ValueError):
        return None
    compiled = compile_plan_candidate(
        retained, context, allow_coverage_gaps=True,
        allow_empty_overview=allow_empty_overview,
    )
    if compiled.delta is None or any(
        issue.code != "coverage_gap" for issue in compiled.issues
    ):
        return None
    omissions.extend(compiled.unassigned)
    unique = list({content_id(value): value for value in omissions}.values())
    return SalvagedPlan(
        retained, compiled.delta, unique, sorted(failed_keys),
        list(compiled.normalizations), "page",
    )
