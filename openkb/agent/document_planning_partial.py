"""Prepare a proof and omission for independently retained planning content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan_annotations import PlanningOmission
from openkb.agent.document_plan_salvage import SalvagedPlan, save_salvage_proof
from openkb.agent.document_planning_ledger_annotations import partial_omission


@dataclass(frozen=True)
class PartialAcceptance:
    omission: PlanningOmission | None
    proof_key: str | None
    normalizations: list[dict[str, Any]] | None


def prepare_partial_acceptance(
    ledger: Any, checkpoints: Any, window: dict[str, Any],
    *, original: Any, candidate: dict[str, Any], delta: dict[str, Any],
    salvaged: SalvagedPlan | None, attempts: int,
    normalizations: list[dict[str, Any]] | None,
) -> PartialAcceptance:
    if salvaged is None:
        return PartialAcceptance(None, None, normalizations)
    omission = (
        partial_omission(
            ledger, window, ranges=salvaged.omission_ranges,
            affected_pages=salvaged.affected_pages, attempts=attempts,
            diagnostic_ref=None, component=salvaged.component,
        ) if salvaged.omission_ranges else None
    )
    proof = save_salvage_proof(
        checkpoints, window, original, candidate, delta, salvaged
    )
    return PartialAcceptance(
        omission, proof, [*(normalizations or []), *salvaged.normalizations]
    )


def report_partial_acceptance(omission: PlanningOmission | None, on_event: Any, index: int) -> None:
    if omission is None:
        return
    from openkb.compilation_report import report_content_omission

    report_content_omission("planning", omission.reason, [omission.key])
    on_event({
        "stage": "planning", "operation": "partial_acceptance",
        "window": index + 1, "omission": omission.key,
    })
