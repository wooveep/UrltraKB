"""Outcome of the planning stage, separate from publication and task status."""

from __future__ import annotations

from dataclasses import dataclass

from openkb.agent.document_plan import DocumentPlan


@dataclass(frozen=True)
class PlanningResult:
    plan: DocumentPlan | None
    outcome: str
    report_ref: str | None

    @classmethod
    def from_plan(cls, plan: DocumentPlan | None, report_ref: str | None = None) -> PlanningResult:
        if plan is None:
            return cls(None, "empty", report_ref)
        return cls(
            plan,
            "partial" if plan.planning_omissions else "complete",
            plan.metadata.get("plan_preview"),
        )
