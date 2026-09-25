"""Validation of accepted DocumentPlan records against immutable source evidence."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from openkb.agent.document_plan import (
    DocumentPlan,
    RangeValue,
    _blocking_page_keys,
    _derived_page_state,
    range_intervals,
)
from openkb.agent.document_plan_annotations import (
    ExternalReference,
    PageLimitation,
    PlanningOmission,
    source_quote,
)

_PAGE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
_OMITTED_PAGE = re.compile(r"^omitted-page:[0-9a-f]{64}$")


def _check_range(r: RangeValue, parsed: Any, context_label: str) -> None:
    # ``range_intervals`` validates both shape and exact character bounds.  It
    # is deliberately the one authority used by planning, generation and
    # coverage rather than allowing each layer to reinterpret partial blocks.
    for index, _, _ in range_intervals(r, parsed, context_label):
        if "attachment" in getattr(parsed.blocks[index], "location", {}):
            raise ValueError(f"Range in {context_label} references unread attachment content")


def validate_plan(
    plan: DocumentPlan,
    parsed: Any,
    allowed_entity_types: list[str],
    existing_targets: set[str],
) -> bool:
    """Validate all ranges, entity types, and targets against immutable parsed blocks."""
    protocol = plan.metadata.get("protocol")
    if protocol not in {"document-plan-v1", "document-plan-v2", "document-plan-v3"}:
        raise ValueError("Invalid DocumentPlan protocol")
    if plan.overview.status not in {"partial", "complete"}:
        raise ValueError("Invalid overview status")
    zero_readable_body = plan.metadata.get("zero_readable_body") is True
    overview_omitted = (
        protocol == "document-plan-v2"
        and plan.overview.status == "partial"
        and any(item.component == "overview" for item in plan.planning_omissions)
        and not plan.overview.text.strip()
        and not plan.overview.ranges
    )
    if (
        protocol != "document-plan-v3"
        and not zero_readable_body
        and not overview_omitted
        and not plan.overview.text.strip()
    ):
        raise ValueError("Readable DocumentPlan needs a non-empty overview")
    if (
        protocol != "document-plan-v3"
        and not zero_readable_body
        and not overview_omitted
        and not plan.overview.ranges
    ):
        raise ValueError("Readable DocumentPlan overview needs exact evidence ranges")
    if zero_readable_body and plan.pages:
        raise ValueError("Zero-readable-body plan cannot contain pages")

    # Validate overview ranges
    for r in plan.overview.ranges:
        _check_range(r, parsed, "overview")

    # Validate pages
    page_keys = set()
    page_names = set()
    for page in plan.pages:
        if not isinstance(page.key, str) or not page.key or page.key in page_keys:
            raise ValueError("Page keys must be unique non-empty strings")
        page_keys.add(page.key)
        if not isinstance(page.name, str) or not page.name or page.name in page_names:
            raise ValueError("Page names must be unique non-empty strings")
        page_names.add(page.name)
        if any(
            not isinstance(value, str)
            for value in (page.title, page.purpose, page.target, page.state, page.quality)
        ):
            raise ValueError(f"Invalid page fields for {page.name}")
        if not page.purpose.strip():
            raise ValueError(f"Page {page.name} needs a non-empty purpose")
        if page.type is not None and not isinstance(page.type, str):
            raise ValueError(f"Invalid entity type for {page.name}")
        if page.local_key is not None and not isinstance(page.local_key, str):
            raise ValueError(f"Invalid local key for {page.name}")
        if page.review_receipt is not None and not isinstance(page.review_receipt, dict):
            raise ValueError(f"Invalid review receipt for {page.name}")
        if page.kind not in {"concept", "entity"}:
            raise ValueError(f"Invalid page kind: {page.kind}")
        _check_page_path(page.name, page.kind)
        if page.target and page.target not in existing_targets:
            raise ValueError(f"Unknown existing page target: {page.target}")
        if page.target and page.target != page.name:
            raise ValueError(f"Existing target must equal page name: {page.target}")
        if (
            page.name in existing_targets
            and not page.target
            and not (
                protocol == "document-plan-v3"
                and page.name not in plan.metadata.get("catalog_targets", [])
            )
        ):
            raise ValueError(f"Existing page name requires its actual target: {page.name}")
        if page.state not in {"ready", "blocked"}:
            raise ValueError(f"Invalid page state: {page.state}")
        if page.quality not in {"planned", "generated", "verified", "unverified", "published"}:
            raise ValueError(f"Invalid page quality: {page.quality}")
        from openkb.agent.document_page_receipts import valid_critical_review_receipt

        if page.quality in {"verified", "published"} and not valid_critical_review_receipt(
            page.review_receipt
        ):
            raise ValueError(f"Invalid critical review receipt for {page.name}")
        if page.quality == "unverified" and not isinstance(page.review_receipt, dict):
            raise ValueError(f"Missing review receipt for {page.name}")
        if page.kind == "entity":
            if not page.type or page.type not in allowed_entity_types:
                raise ValueError(
                    f"Invalid entity type '{page.type}' for page '{page.name}', "
                    f"allowed: {allowed_entity_types}"
                )
        if not isinstance(page.subject_ranges, list) or not page.subject_ranges:
            raise ValueError(f"Page {page.name} needs exact subject_ranges")
        for r in page.subject_ranges:
            _check_range(r, parsed, f"page {page.name} subject_ranges")
        if protocol == "document-plan-v3":
            if page.scope_resolution not in {"section", "explicit_range", "target_fallback"}:
                raise ValueError(f"Invalid scope resolution for {page.name}")
            if not all(isinstance(note, str) for note in page.planning_notes):
                raise ValueError(f"Invalid planning note for {page.name}")
            for r in page.context_ranges:
                _check_range(r, parsed, f"page {page.name} context_ranges")
        for ctx in page.necessary_context:
            if not isinstance(ctx, dict):
                raise ValueError(f"Invalid necessary_context in page {page.name}")
            required = {"relation", "basis", "ranges", "basis_ranges"}
            if not required <= set(ctx) or not set(ctx) <= required | {"basis_quote", "rationale"}:
                raise ValueError(f"Invalid necessary_context in page {page.name}")
            if "basis_quote" in ctx and ctx["basis_quote"] != ctx["basis"]:
                raise ValueError(f"Invalid necessary_context in page {page.name}")
            rel = ctx.get("relation")
            if rel not in {"explicit_reference", "applicable_condition"}:
                raise ValueError(f"Invalid relation in necessary_context: {rel}")
            if not isinstance(ctx.get("basis", ""), str) or not ctx.get("basis", "").strip():
                raise ValueError(f"Necessary context in {page.name} needs an original-text basis")
            ranges = ctx.get("ranges", [])
            basis_ranges = ctx.get("basis_ranges", [])
            if not isinstance(ranges, list) or not ranges:
                raise ValueError(f"Necessary context in {page.name} needs exact ranges")
            if not isinstance(basis_ranges, list) or not basis_ranges:
                raise ValueError(f"Necessary context in {page.name} needs exact basis_ranges")
            for r in ranges:
                _check_range(r, parsed, f"page {page.name} necessary_context")
            for r in basis_ranges:
                _check_range(r, parsed, f"page {page.name} necessary_context basis")
        if protocol in {"document-plan-v2", "document-plan-v3"}:
            from openkb.agent.document_range_validation import interval_is_covered, merged_intervals

            visible: dict[int, list[tuple[int, int]]] = {}
            for value in [
                *page.subject_ranges,
                *(r for ctx in page.necessary_context for r in ctx["ranges"]),
            ]:
                for index, start, end in range_intervals(value, parsed, "page evidence"):
                    visible.setdefault(index, []).append((start, end))
            visible = {index: merged_intervals(spans) for index, spans in visible.items()}
            for limitation in page.limitations:
                if not isinstance(limitation, PageLimitation) or not limitation.ranges:
                    raise ValueError(f"Invalid limitation for {page.name}")
                for value in limitation.ranges:
                    _check_range(value, parsed, f"page {page.name} limitation")
                    if any(
                        not interval_is_covered(index, start, end, visible)
                        for index, start, end in range_intervals(value, parsed, "page limitation")
                    ):
                        raise ValueError(f"Limitation outside page evidence for {page.name}")
                if all(
                    hasattr(parsed.blocks[index], "text")
                    for value in limitation.ranges
                    for index, _, _ in range_intervals(value, parsed, "page limitation")
                ) and limitation.source_quote != source_quote(
                    limitation.ranges, parsed, "page limitation"
                ):
                    raise ValueError(f"Invalid limitation source quote for {page.name}")
        elif page.limitations:
            raise ValueError("Legacy DocumentPlan cannot contain page limitations")

    if any(page.quality == "published" for page in plan.pages) and not isinstance(
        plan.metadata.get("publication_receipt"), dict
    ):
        raise ValueError("Published pages need a completed publication receipt")

    # Validate source_only
    for item in plan.source_only:
        if not isinstance(item.reason, str) or not item.reason.strip():
            raise ValueError("source_only item must have a non-empty reason")
        if not isinstance(item.ranges, list):
            raise ValueError("source_only item ranges must be a list")
        for r in item.ranges:
            _check_range(r, parsed, "source_only")

    # Validate unresolved
    unresolved_keys = set()
    for u in plan.unresolved:
        if not isinstance(u.key, str) or not u.key or u.key in unresolved_keys:
            raise ValueError("Unresolved keys must be unique non-empty strings")
        unresolved_keys.add(u.key)
        if u.status not in {"open", "resolved"} or type(u.blocking) is not bool:
            raise ValueError(f"Invalid unresolved status for {u.key}")
        if (
            not all(
                isinstance(value, str) and value.strip()
                for value in (u.problem_type, u.missing_target, u.reason)
            )
            or not isinstance(u.affected_pages, list)
            or any(not isinstance(page, str) for page in u.affected_pages)
        ):
            raise ValueError(f"Unresolved item {u.key} is missing required fields")
        if u.problem_type not in {
            "missing_prerequisite",
            "unresolved_cross_reference",
            "missing_external_material",
            "parsing_limitation",
        }:
            raise ValueError(f"Unresolved item {u.key} has an invalid problem type")
        if not u.affected_pages or any(page not in page_keys for page in u.affected_pages):
            raise ValueError(f"Unresolved item {u.key} has invalid affected pages")
        if not u.location:
            raise ValueError(f"Unresolved item {u.key} needs an exact location")
        for r in u.location:
            _check_range(r, parsed, f"unresolved {u.key}")

    # Validate resolutions
    resolved = set()
    for res in plan.resolutions:
        if (
            not isinstance(res.unresolved_key, str)
            or res.unresolved_key not in unresolved_keys
            or res.unresolved_key in resolved
            or not isinstance(res.basis_ranges, list)
            or not res.basis_ranges
        ):
            raise ValueError("Resolution must refer to one unique unresolved item")
        resolved.add(res.unresolved_key)
        for r in res.basis_ranges:
            _check_range(r, parsed, f"resolution for {res.unresolved_key}")
    if resolved != {item.key for item in plan.unresolved if item.status == "resolved"}:
        raise ValueError("Resolved unresolved items need one exact resolution receipt")

    blocked_keys = _blocking_page_keys(plan.unresolved)
    for page in plan.pages:
        expected_state = _derived_page_state(page, blocked_keys)
        if page.state != expected_state:
            raise ValueError(f"Page {page.name} state must equal its derived state")

    if protocol in {"document-plan-v2", "document-plan-v3"}:
        reference_keys: set[str] = set()
        for reference in plan.external_references:
            if (
                not isinstance(reference, ExternalReference)
                or reference.key in reference_keys
                or not reference.key.startswith("xref:")
                or not reference.location
                or any(key not in page_keys for key in reference.affected_pages)
            ):
                raise ValueError("Invalid external reference")
            reference_keys.add(reference.key)
            for value in reference.location:
                _check_range(value, parsed, "external reference")
            if all(
                hasattr(parsed.blocks[index], "text")
                for value in reference.location
                for index, _, _ in range_intervals(value, parsed, "external reference")
            ) and reference.raw_quote != source_quote(
                reference.location, parsed, "external reference"
            ):
                raise ValueError("Invalid external reference source quote")
    elif plan.external_references:
        raise ValueError("Legacy DocumentPlan cannot contain external references")

    if protocol in {"document-plan-v2", "document-plan-v3"}:
        omission_keys: set[str] = set()
        for omission in plan.planning_omissions:
            if (
                not isinstance(omission, PlanningOmission)
                or omission.key in omission_keys
                or any(
                    page not in page_keys and _OMITTED_PAGE.fullmatch(page) is None
                    for page in omission.affected_pages
                )
            ):
                raise ValueError("Invalid planning omission")
            omission_keys.add(omission.key)
            for value in omission.ranges:
                _check_range(value, parsed, "planning omission")
    elif plan.planning_omissions:
        raise ValueError("Legacy DocumentPlan cannot contain planning omissions")

    return True


def _check_page_path(name: str, kind: str) -> None:
    path = PurePosixPath(name)
    folder = "concepts" if kind == "concept" else "entities"
    if (
        path.is_absolute()
        or path.parts[:1] != (folder,)
        or len(path.parts) != 2
        or not _PAGE_SEGMENT.fullmatch(path.name)
    ):
        raise ValueError(f"Invalid {kind} page path: {name}")
