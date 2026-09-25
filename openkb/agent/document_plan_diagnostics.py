"""Safe, read-only coverage diagnostics for incomplete plan candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from openkb.agent.document_plan_issues import PlanValidationError, ValidationIssue
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_range_validation import (
    require_nonempty_ranges,
    target_coverage_issues,
    validate_evidence_ranges,
    validate_ranges,
    validate_target_ranges,
)


@dataclass(frozen=True)
class CoverageDiagnostics:
    status: str
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class PreparedCandidate:
    canonical: Any
    issues: tuple[ValidationIssue, ...]
    coverage_status: str
    normalizations: tuple[dict[str, Any], ...] = ()


def prepare_candidate(
    candidate: Any,
    context: Any,
    target: dict[int, list[tuple[int, int]]],
    evidence: dict[int, list[tuple[int, int]]],
    chars: list[int],
    ignored: set[int],
) -> PreparedCandidate:
    """Resolve v4 identities, then diagnose only safely known coverage."""
    selected_issues: tuple[ValidationIssue, ...] = ()
    normalizations: tuple[dict[str, Any], ...] = ()
    available = candidate
    if context.selection_protocol in {"document-plan-v4", "document-plan-v5"}:
        resolver = SelectionResolver(
            list(context.evidence.get("blocks", [])), chars, target, evidence,
            list(context.navigation_hints), context.selection_protocol,
        )
        selected = resolver.resolve_candidate(candidate)
        candidate, available, selected_issues = (
            selected.canonical,
            selected.partial,
            selected.issues,
        )
        normalizations = selected.normalizations
    coverage = inspect_coverage(
        available, target=target, evidence=evidence, chars=chars, ignored=ignored
    )
    status = "unchecked" if selected_issues else coverage.status
    coverage_issues = tuple(
        replace(issue, code="coverage_pending", blocking=False)
        if selected_issues and issue.code == "coverage_gap"
        else issue
        for issue in coverage.issues
    )
    return PreparedCandidate(
        candidate, (*selected_issues, *coverage_issues), status, normalizations
    )


def inspect_coverage(
    candidate: Any,
    *,
    target: dict[int, list[tuple[int, int]]],
    evidence: dict[int, list[tuple[int, int]]],
    chars: list[int],
    ignored: set[int],
) -> CoverageDiagnostics:
    """Validate each route independently, preserving original list positions."""
    if not isinstance(candidate, dict) or any(
        not isinstance(candidate.get(section), list)
        for section in ("page_changes", "source_only", "unresolved")
    ):
        return CoverageDiagnostics("unchecked", ())

    problems: list[ValidationIssue] = []
    invalid = False
    missing_context = False

    def checked_ranges(values: Any, path: str, *, target_only: bool) -> list[Any]:
        nonlocal invalid
        try:
            validate_ranges(
                values,
                len(chars),
                path,
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=path,
            )
            require_nonempty_ranges(values, path, path)
            if target_only:
                validate_target_ranges(values, target, path, chars, field_path=path)
            else:
                validate_evidence_ranges(values, evidence, path, chars, field_path=path)
        except PlanValidationError as exc:
            problems.extend(exc.issues)
            invalid = True
            return []
        return values

    pages: list[dict[str, Any]] = []
    for index, page in enumerate(candidate["page_changes"]):
        if not isinstance(page, dict):
            invalid = True
            pages.append({"subject_ranges": [], "necessary_context": []})
            continue
        prefix = f"page_changes[{index}]"
        subject = checked_ranges(
            page.get("subject_ranges"), f"{prefix}.subject_ranges", target_only=True
        )
        contexts: list[dict[str, Any]] = []
        if "necessary_context" not in page:
            missing_context = True
            raw_contexts = []
        else:
            raw_contexts = page["necessary_context"]
        if not isinstance(raw_contexts, list):
            invalid = True
            raw_contexts = []
        for context_index, item in enumerate(raw_contexts):
            if not isinstance(item, dict):
                invalid = True
                continue
            context_prefix = f"{prefix}.necessary_context[{context_index}]"
            contexts.append(
                {
                    field: checked_ranges(
                        item.get(field), f"{context_prefix}.{field}", target_only=False
                    )
                    for field in ("ranges", "basis_ranges")
                }
            )
        pages.append({"subject_ranges": subject, "necessary_context": contexts})

    source_only: list[dict[str, Any]] = []
    for index, item in enumerate(candidate["source_only"]):
        if not isinstance(item, dict):
            invalid = True
            source_only.append({"ranges": []})
            continue
        source_only.append(
            {
                "ranges": checked_ranges(
                    item.get("ranges"), f"source_only[{index}].ranges", target_only=True
                )
            }
        )

    unresolved: list[dict[str, Any]] = []
    for index, item in enumerate(candidate["unresolved"]):
        if not isinstance(item, dict):
            invalid = True
            unresolved.append({"location": []})
            continue
        unresolved.append(
            {
                "location": checked_ranges(
                    item.get("location"), f"unresolved[{index}].location", target_only=True
                )
            }
        )

    coverage = target_coverage_issues(
        target, pages, source_only, unresolved, block_chars=chars, ignored_blocks=ignored
    )
    problems.extend(issue for issue in coverage if issue.code == "source_only_conflict")
    if invalid or missing_context:
        problems.extend(
            replace(
                issue,
                code="coverage_pending",
                blocking=False,
                expected=(
                    "Route this range only if invalid or missing fields do not already cover it; "
                    "use subject, context, source_only, or unresolved"
                ),
            )
            for issue in coverage
            if issue.code == "coverage_gap"
        )
        return CoverageDiagnostics("unchecked" if invalid else "provisional", tuple(problems))
    problems.extend(issue for issue in coverage if issue.code == "coverage_gap")
    return CoverageDiagnostics("checked", tuple(problems))
