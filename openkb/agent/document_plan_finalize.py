"""Finish a compiled planning delta after its independent fields are checked."""

from __future__ import annotations

from typing import Any

from openkb.agent._document_plan_compiler_support import PlanCompileResult, issue, run_validation
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
    if context.retry_page_keys is not None:
        allowed = context.retry_page_keys
        for index, page in enumerate(delta["page_changes"]):
            if page["target_key"] not in allowed:
                issues.append(issue(
                    "retry_page_scope", f"page_changes[{index}].target_key",
                    sorted(allowed), page["target_key"],
                    item_ref=f"page:{page['local_key']}",
                ))
        for section in ("unresolved", "external_references"):
            for index, row in enumerate(delta.get(section, [])):
                if not set(row["affected_pages"]) <= allowed:
                    issues.append(issue(
                        "retry_page_scope", f"{section}[{index}].affected_pages",
                        sorted(allowed), row["affected_pages"],
                        item_ref=f"{section}:{index}",
                    ))
        open_issues = {
            row["key"]: row for row in context.open_unresolved
            if isinstance(row, dict) and isinstance(row.get("key"), str)
        }
        for index, resolution in enumerate(delta["resolutions"]):
            prior = open_issues.get(resolution["unresolved_key"])
            if prior is None or not set(prior["affected_pages"]) <= allowed:
                issues.append(issue(
                    "retry_page_scope", f"resolutions[{index}].unresolved_key",
                    sorted(allowed), resolution["unresolved_key"],
                    item_ref=f"resolution:{index}",
                ))
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
