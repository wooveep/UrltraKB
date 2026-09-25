"""Finish a compiled planning delta after its independent fields are checked."""

from __future__ import annotations

from typing import Any

from openkb.agent._document_plan_compiler_support import PlanCompileResult, run_validation
from openkb.agent.document_planning_support import canonicalize_context_bases


def finalize_candidate(
    raw_candidate: Any,
    delta: dict[str, Any],
    issues: list[Any],
    coverage_issues: tuple[Any, ...],
    coverage_status: str,
    context: Any,
    normalizations: list[dict[str, Any]],
    *,
    allow_coverage_gaps: bool = False,
) -> PlanCompileResult:
    issues.extend(
        issue for issue in coverage_issues
        if issue.code in {"source_only_conflict", "coverage_gap", "coverage_pending"}
    )
    if not issues or allow_coverage_gaps and all(issue.code == "coverage_gap" for issue in issues):
        run_validation(
            issues, "$",
            lambda: canonicalize_context_bases(delta, dict(context.evidence), context.parsed),
        )
    unassigned = tuple(
        source_range
        for issue in issues if issue.code == "coverage_gap"
        for source_range in issue.source_ranges
    )
    blocking = any(
        issue.blocking and (not allow_coverage_gaps or issue.code != "coverage_gap")
        for issue in issues
    )
    accepted_delta = None if blocking or coverage_status != "checked" else delta
    return PlanCompileResult(
        raw_candidate, tuple(issues), unassigned, accepted_delta, coverage_status,
        tuple(normalizations),
    )
