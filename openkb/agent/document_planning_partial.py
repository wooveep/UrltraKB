"""Prepare a proof and omission for independently retained planning content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan import UnresolvedItem
from openkb.agent.document_plan_annotations import PlanningOmission
from openkb.agent.document_plan_salvage import SalvagedPlan, save_salvage_proof
from openkb.agent.document_planning_ledger_annotations import partial_omission
from openkb.sources import content_id


@dataclass(frozen=True)
class PartialAcceptance:
    omissions: list[PlanningOmission]
    proof_key: str | None
    normalizations: list[dict[str, Any]] | None


def combine_salvage(
    prior: SalvagedPlan | None, reference: SalvagedPlan | None,
    candidate: dict[str, Any], delta: dict[str, Any],
) -> SalvagedPlan | None:
    if prior is None and reference is None:
        return None
    parts = [part for part in (prior, reference) if part is not None]
    omissions = list({
        content_id(value): value
        for part in parts for value in part.omission_ranges
    }.values())
    return SalvagedPlan(
        candidate, delta, omissions,
        sorted({page for part in parts for page in part.affected_pages}),
        [row for part in parts for row in part.normalizations],
        parts[0].component if len(parts) == 1 else "document_plan",
    )


def prepare_partial_acceptance(
    ledger: Any, checkpoints: Any, window: dict[str, Any],
    *, original: Any, candidate: dict[str, Any], delta: dict[str, Any],
    salvaged: SalvagedPlan | None, attempts: int,
    normalizations: list[dict[str, Any]] | None,
) -> PartialAcceptance:
    blocked_ranges, blocked_pages = _blocked_page_ranges(ledger, delta)
    omissions = []
    if salvaged is not None and salvaged.omission_ranges:
        omissions.append(partial_omission(
            ledger, window, ranges=salvaged.omission_ranges,
            affected_pages=salvaged.affected_pages, attempts=attempts,
            diagnostic_ref=None, component=salvaged.component,
        ))
    retry_key = window.get("retry_omission_key")
    retry_row = ledger.db.execute(
        "SELECT payload FROM planning_omissions WHERE key = ?", (retry_key,)
    ).fetchone() if retry_key else None
    retry_blocker = retry_row is not None and PlanningOmission.from_dict(
        json.loads(retry_row[0])
    ).reason == "document_plan_blocked_page"
    if blocked_ranges and not retry_blocker:
        omissions.append(partial_omission(
            ledger, window, ranges=blocked_ranges,
            affected_pages=blocked_pages, attempts=attempts,
            diagnostic_ref=None, component="blocked_page",
            reason="document_plan_blocked_page", durable_pages=True,
        ))
    proof = (
        save_salvage_proof(checkpoints, window, original, candidate, delta, salvaged)
        if salvaged is not None else None
    )
    return PartialAcceptance(
        omissions, proof, [*(normalizations or []), *(salvaged.normalizations if salvaged else [])]
    )


def _blocked_page_ranges(ledger: Any, delta: dict[str, Any]) -> tuple[list[Any], list[str]]:
    """Register only current-target evidence for blocked durable pages."""
    changes = {row["target_key"]: row for row in delta["page_changes"]}
    blocked = {row["target_key"] for row in changes.values() if row.get("state") == "blocked"}
    blocked.update(
        page for issue in delta["unresolved"] if issue.get("status") == "open"
        and issue.get("blocking", True) for page in issue["affected_pages"]
    )
    resolved = {row["unresolved_key"] for row in delta["resolutions"]}
    for (payload,) in ledger.db.execute(
        "SELECT payload FROM unresolved WHERE status = 'open'"
    ):
        issue = UnresolvedItem.from_dict(json.loads(payload))
        if issue.key not in resolved and issue.blocking:
            blocked.update(key for key in changes if key in issue.affected_pages)
    ranges: list[Any] = [
        value for issue in delta["unresolved"]
        if issue.get("status") == "open" and issue.get("blocking", True)
        for value in issue["location"]
    ]
    for key in sorted(blocked):
        page = changes.get(key)
        if page is not None:
            ranges.extend(page["subject_ranges"])
    return list({content_id(value): value for value in ranges}.values()), sorted(blocked)


def report_partial_acceptance(omissions: list[PlanningOmission], on_event: Any, index: int) -> None:
    from openkb.compilation_report import report_content_omission

    for omission in omissions:
        report_content_omission("planning", omission.reason, [omission.key])
        on_event({
            "stage": "planning", "operation": "partial_acceptance",
            "window": index + 1, "omission": omission.key,
        })
