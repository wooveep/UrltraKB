"""Revalidate independent planning items after bounded candidate repair fails."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan_compiler import PlanningContext, compile_plan_candidate
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_window_receipts import target_ranges, window_receipt_id
from openkb.sources import content_id

_ITEM = re.compile(
    r"^(page_changes|source_only|unresolved|resolutions|external_references)\[(\d+)\]"
)


@dataclass(frozen=True)
class SalvagedPlan:
    candidate: dict[str, Any]
    delta: dict[str, Any]
    omission_ranges: list[Any]
    affected_pages: list[str]
    normalizations: list[dict[str, Any]]
    component: str = "document_plan"


def salvage_candidate(
    raw: Any, context: PlanningContext, window: dict[str, Any]
) -> SalvagedPlan | None:
    """Keep only complete independently checked rows; never invent a content route."""
    if not isinstance(raw, dict):
        return None
    candidate = deepcopy(raw)
    resolver = SelectionResolver.from_context(context)
    omissions: list[Any] = []
    affected_pages: set[str] = set()
    normalizations: list[dict[str, Any]] = []
    overview_missing = False
    fallback = target_ranges(window)

    def omitted_ranges(row: Any, field: str) -> None:
        values = row.get(field) if isinstance(row, dict) else None
        try:
            decoded = resolver.decode_ranges(values, "salvage", target_only=True)
        except (KeyError, TypeError, ValueError):
            decoded = fallback
        omissions.extend(decoded or fallback)

    for _ in range(1 + sum(len(value) for value in candidate.values() if isinstance(value, list))):
        result = compile_plan_candidate(
            candidate, context, allow_coverage_gaps=True,
            allow_empty_overview=overview_missing,
        )
        if result.delta is not None and all(
            issue.code == "coverage_gap" for issue in result.issues
        ):
            omissions.extend(result.unassigned)
            if not any(
                candidate.get(name) for name in ("page_changes", "source_only", "unresolved")
            ):
                return None
            unique = list({content_id(value): value for value in omissions}.values())
            return SalvagedPlan(
                candidate, result.delta, unique, sorted(affected_pages),
                [*normalizations, *result.normalizations],
                "overview" if overview_missing else "document_plan",
            )
        to_drop: dict[str, set[int]] = {}
        cleared_overview = False
        for issue in result.issues:
            if issue.code in {"coverage_gap", "coverage_pending"}:
                continue
            match = _ITEM.match(issue.path)
            if match is None:
                if issue.path.startswith("overview.") and not overview_missing:
                    overview_missing = True
                    cleared_overview = True
                    candidate["overview"] = {"text": "", "ranges": [], "limitations": []}
                    omissions.extend(fallback)
                    normalizations.append({
                        "code": "failed_overview_excluded", "path": "overview"
                    })
                    continue
                return None
            section, index = match.group(1), int(match.group(2))
            if index >= len(candidate.get(section, [])):
                return None
            to_drop.setdefault(section, set()).add(index)
        if not to_drop and cleared_overview:
            continue
        if not to_drop:
            return None
        dropped_local = {
            candidate["page_changes"][index].get("local_key")
            for index in to_drop.get("page_changes", ())
            if isinstance(candidate["page_changes"][index], dict)
        }
        for index, row in enumerate(candidate.get("unresolved", [])):
            if isinstance(row, dict) and dropped_local.intersection(row.get("affected_pages", [])):
                to_drop.setdefault("unresolved", set()).add(index)
        for index in to_drop.get("unresolved", ()):
            row = candidate["unresolved"][index]
            if isinstance(row, dict):
                affected_pages.update(row.get("affected_pages", []))
                dropped_local.update(row.get("affected_pages", []))
        for index, row in enumerate(candidate.get("page_changes", [])):
            if isinstance(row, dict) and row.get("local_key") in dropped_local:
                to_drop.setdefault("page_changes", set()).add(index)
        for section, indices in to_drop.items():
            for index in sorted(indices, reverse=True):
                row = candidate[section].pop(index)
                if section == "page_changes":
                    affected_pages.add(row.get("local_key", ""))
                    omitted_ranges(row, "subject_ranges")
                elif section == "source_only":
                    omitted_ranges(row, "ranges")
                elif section == "unresolved":
                    omitted_ranges(row, "location")
                elif section == "resolutions":
                    omissions.extend(fallback)
                normalizations.append({
                    "code": "failed_item_excluded", "path": f"{section}[{index}]"
                })
        if dropped_local:
            for reference in candidate.get("external_references", []):
                if isinstance(reference, dict) and isinstance(
                    reference.get("affected_pages"), list
                ):
                    reference["affected_pages"] = [
                        key for key in reference["affected_pages"] if key not in dropped_local
                    ]
    return None


def save_salvage_proof(
    checkpoints: Any, window: dict[str, Any], original: Any,
    candidate: dict[str, Any], delta: dict[str, Any], salvaged: SalvagedPlan,
) -> str:
    """Anchor the rejected complete candidate and the independently checked subset."""
    payload = {
        "stage": "document-plan-salvage", "protocol": "document-plan-salvage-v1",
        "window": window_receipt_id(window), "original": content_id(original),
        "candidate": content_id(candidate), "delta": content_id(delta),
    }
    key = checkpoints.key("DocumentPlan salvaged candidate", payload)
    checkpoints.save(key, {
        **payload, "original_candidate": original, "accepted_candidate": candidate,
        "omission_ranges": salvaged.omission_ranges,
        "affected_pages": salvaged.affected_pages,
        "normalizations": salvaged.normalizations,
    })
    return key
