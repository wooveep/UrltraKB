"""Outcome of the planning stage, separate from publication and task status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan import DocumentPlan


@dataclass(frozen=True)
class PlanningResult:
    plan: DocumentPlan | None
    outcome: str
    report_ref: str | None
    overview_ref: str | None = None
    planning_omissions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_plan(cls, plan: DocumentPlan | None, report_ref: str | None = None) -> PlanningResult:
        if plan is None:
            return cls(None, "empty", report_ref)
        return cls(
            plan,
            plan.metadata.get("outcome") or ("partial" if plan.planning_omissions else "complete"),
            report_ref or plan.metadata.get("plan_report") or plan.metadata.get("plan_preview"),
            plan.metadata.get("overview_ref"),
            tuple(item.to_dict() for item in plan.planning_omissions),
        )
