"""Compile model-selected limitations and external mentions against supplied source text."""

from __future__ import annotations

from typing import Any

from openkb.agent._document_plan_compiler_support import issue, run_validation
from openkb.agent.document_plan_annotations import quote_from_evidence
from openkb.agent.document_plan_issues import ValidationIssue
from openkb.agent.document_range_validation import (
    interval_is_covered,
    merged_intervals,
    range_intervals,
    require_nonempty_ranges,
    validate_evidence_ranges,
    validate_ranges,
)
from openkb.sources import content_id


def compile_page_limitations(
    raw: Any,
    *,
    path: str,
    subject: list[Any],
    contexts: list[dict[str, Any]],
    total_blocks: int,
    chars: list[int],
    ignored: set[int],
    evidence: dict[int, list[tuple[int, int]]],
    supplied: Any,
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """Keep only limitations whose exact source is readable by this page."""
    result: list[dict[str, Any]] = []
    issues: list[ValidationIssue] = []
    visible: dict[int, list[tuple[int, int]]] = {}
    for value in [*subject, *(value for row in contexts for value in row["ranges"])]:
        for index, start, end in range_intervals(value, block_chars=chars):
            visible.setdefault(index, []).append((start, end))
    visible = {index: merged_intervals(rows) for index, rows in visible.items()}
    for index, item in enumerate(raw):
        item_path = f"{path}.limitations[{index}]"
        item_ref = f"limitation:{path}:{index}"
        ranges, reason = item.get("ranges"), item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(
                issue(
                    "invalid_text",
                    f"{item_path}.reason",
                    "nonempty string",
                    reason,
                    item_ref=item_ref,
                )
            )
        if run_validation(
            issues,
            item_ref,
            lambda: validate_ranges(
                ranges,
                total_blocks,
                "page limitation",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{item_path}.ranges",
            ),
        ):
            run_validation(
                issues,
                item_ref,
                lambda: require_nonempty_ranges(ranges, f"{item_path}.ranges", "Page limitation"),
            )
            run_validation(
                issues,
                item_ref,
                lambda: validate_evidence_ranges(
                    ranges,
                    evidence,
                    "page limitation",
                    chars,
                    field_path=f"{item_path}.ranges",
                    item_ref=item_ref,
                ),
            )
            for value in ranges:
                for block, start, end in range_intervals(value, block_chars=chars):
                    if not interval_is_covered(block, start, end, visible):
                        issues.append(
                            issue(
                                "limitation_outside_page_evidence",
                                f"{item_path}.ranges",
                                "page subject or necessary context",
                                ranges,
                                item_ref=item_ref,
                                category="evidence",
                            )
                        )
                        break
            try:
                quote = quote_from_evidence(ranges, supplied, chars)
            except ValueError:
                issues.append(
                    issue(
                        "limitation_outside_evidence",
                        f"{item_path}.ranges",
                        "supplied original text",
                        ranges,
                        item_ref=item_ref,
                        category="evidence",
                    )
                )
                continue
            result.append({"ranges": ranges, "reason": reason, "source_quote": quote})
    return result, issues


def compile_external_references(
    raw: Any,
    *,
    pages: list[dict[str, Any]],
    context: Any,
    allocated_local: dict[str, str],
    known_page_keys: set[str],
    chars: list[int],
    ignored: set[int],
    evidence: dict[int, list[tuple[int, int]]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """Bind literal external clues to the current source and accepted page identities."""
    issues: list[ValidationIssue] = []
    result: list[dict[str, Any]] = []
    page_by_key = {page["target_key"]: page for page in pages}
    for index, item in enumerate(raw):
        path, item_ref = f"external_references[{index}]", f"external_reference:{index}"
        location = item.get("location")
        if not run_validation(
            issues,
            item_ref,
            lambda: validate_ranges(
                location,
                context.total_blocks,
                "external reference",
                block_chars=chars,
                ignored_blocks=ignored,
                field_path=f"{path}.location",
            ),
        ):
            continue
        if not run_validation(
            issues,
            item_ref,
            lambda: require_nonempty_ranges(location, f"{path}.location", "External reference"),
        ):
            continue
        if not run_validation(
            issues,
            item_ref,
            lambda: validate_evidence_ranges(
                location,
                evidence,
                "external reference",
                chars,
                field_path=f"{path}.location",
                item_ref=item_ref,
            ),
        ):
            continue
        try:
            quote = quote_from_evidence(location, context.evidence, chars)
        except ValueError:
            issues.append(
                issue(
                    "reference_outside_evidence",
                    f"{path}.location",
                    "supplied original text",
                    location,
                    item_ref=item_ref,
                    category="evidence",
                )
            )
            continue
        target_document, target_section = item.get("target_document"), item.get("target_section")
        if any(
            value is not None and not isinstance(value, str)
            for value in (target_document, target_section)
        ):
            issues.append(
                issue(
                    "invalid_reference_target",
                    path,
                    "literal strings or null",
                    [target_document, target_section],
                    item_ref=item_ref,
                )
            )
            continue
        if any(
            value and value.casefold() not in quote.casefold()
            for value in (target_document, target_section)
        ):
            issues.append(
                issue(
                    "reference_target_not_literal",
                    path,
                    "target wording in original quote",
                    [target_document, target_section],
                    item_ref=item_ref,
                    category="evidence",
                )
            )
            continue
        affected_raw = item.get("affected_pages")
        if not isinstance(affected_raw, list) or any(
            not isinstance(value, str) for value in affected_raw
        ):
            issues.append(
                issue(
                    "invalid_affected_pages",
                    f"{path}.affected_pages",
                    "list of supplied page keys",
                    affected_raw,
                    item_ref=item_ref,
                )
            )
            continue
        affected = [allocated_local.get(value, value) for value in affected_raw]
        if any(value not in known_page_keys for value in affected):
            issues.append(
                issue(
                    "unknown_page_reference",
                    f"{path}.affected_pages",
                    "supplied page identities",
                    affected_raw,
                    item_ref=item_ref,
                    category="reference",
                )
            )
            continue
        outside_page = False
        for page_key in affected:
            page = page_by_key.get(page_key)
            if page is None:
                continue
            visible: dict[int, list[tuple[int, int]]] = {}
            ranges = page.get("subject_ranges")
            selected = list(ranges) if isinstance(ranges, list) else []
            for row in page.get("necessary_context") or []:
                values = row.get("ranges") if isinstance(row, dict) else None
                if isinstance(values, list):
                    selected.extend(values)
            for value in selected:
                try:
                    intervals = range_intervals(value, block_chars=chars)
                except (KeyError, TypeError, ValueError, IndexError):
                    continue  # The page validator already records malformed ranges.
                for block, start, end in intervals:
                    visible.setdefault(block, []).append((start, end))
            visible = {block: merged_intervals(rows) for block, rows in visible.items()}
            if any(
                not interval_is_covered(block, start, end, visible)
                for value in location
                for block, start, end in range_intervals(value, block_chars=chars)
            ):
                outside_page = True
                issues.append(
                    issue(
                        "reference_outside_page_evidence",
                        f"{path}.location",
                        "affected page subject or necessary context",
                        location,
                        item_ref=item_ref,
                        category="evidence",
                    )
                )
        if outside_page:
            continue
        key = "xref:" + content_id(
            [
                context.source_version,
                context.parse_identity,
                location,
                target_document,
                target_section,
            ]
        )
        result.append(
            {
                "key": key,
                "location": location,
                "raw_quote": quote,
                "target_document": target_document,
                "target_section": target_section,
                "affected_pages": sorted(set(affected)),
            }
        )
    return result, issues


def attach_annotations(
    candidate: dict[str, Any],
    pages: list[dict[str, Any]],
    context: Any,
    allocated_local: dict[str, str],
    known_page_keys: set[str],
    chars: list[int],
    ignored: set[int],
    evidence: dict[int, list[tuple[int, int]]],
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """Bind all v5 annotations after page identities and evidence are known."""
    issues: list[ValidationIssue] = []
    by_local = {page["local_key"]: page for page in pages}
    for index, change in enumerate(candidate["page_changes"]):
        page = by_local.get(change.get("local_key"))
        if page is None:
            continue
        raw = change.get("limitations", [])
        page["limitations"] = []
        if not raw:
            continue
        try:
            compiled, problems = compile_page_limitations(
                raw,
                path=f"page_changes[{index}]",
                subject=page["subject_ranges"],
                contexts=page["necessary_context"],
                total_blocks=context.total_blocks,
                chars=chars,
                ignored=ignored,
                evidence=evidence,
                supplied=context.evidence,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            issues.append(
                issue(
                    "invalid_page_limitation",
                    f"page_changes[{index}].limitations",
                    "readable page evidence",
                    str(exc),
                    item_ref=f"page:{page['local_key']}",
                )
            )
            continue
        page["limitations"] = compiled
        issues.extend(problems)
    references, reference_issues = compile_external_references(
        candidate.get("external_references", []),
        pages=pages,
        context=context,
        allocated_local=allocated_local,
        known_page_keys=known_page_keys,
        chars=chars,
        ignored=ignored,
        evidence=evidence,
    )
    issues.extend(reference_issues)
    return references, issues
