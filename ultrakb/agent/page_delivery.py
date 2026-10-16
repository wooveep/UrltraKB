"""Validate delivered page data before any publication or repair request."""

import json
from collections.abc import Callable

from ultrakb.agent.page_response import PageResponseError, parse_page, parse_summary
from ultrakb.agent.page_updates import PageUpdate, UnchangedPage


def validate_page_delivery(
    raw: str,
    *,
    response_protocol: str = "page-json",
    expected_sections: tuple[str, ...] = (),
    original_excerpts=None,
    page_update: PageUpdate | None = None,
    read_current: Callable[[], str] | None = None,
    proposal_only: bool = False,
    figure_context=None,
    expected_coverage: tuple[str, ...] = (),
) -> str | UnchangedPage:
    """Use originals to validate one response; no model calls or writes occur here."""
    if response_protocol not in {
        "page-json",
        "markdown",
        "summary-json",
        "summary-page-json",
        "summary-parts-json",
    }:
        raise ValueError("Unknown page response protocol")
    if response_protocol == "summary-parts-json" and original_excerpts is None:
        raise ValueError("Structured summaries require original source windows")
    if page_update is not None and read_current is None:
        raise ValueError("A page update requires its current page reader")
    try:
        if original_excerpts is not None:
            raw = original_excerpts.response(
                raw,
                response_protocol,
                expected_sections,
                update=page_update is not None,
                previous_description=(
                    page_update.request()["description"] if page_update is not None else None
                ),
            )
    finally:
        # Check after proof validation, including rejection: a concurrent human
        # edit is terminal and must never consume a generated-response repair.
        if page_update is not None:
            assert read_current is not None
            current = read_current()
            page_update.require_current(current)
    if page_update is not None:
        result = page_update.apply(raw, current=current)
        if isinstance(result, UnchangedPage):
            return result
        if proposal_only:
            return raw
        raw = json.dumps(result, ensure_ascii=False)
    if response_protocol in {"summary-json", "summary-parts-json"}:
        decoded = parse_summary(raw, expected_sections)
        content = "\n\n".join(section["content"] for section in decoded["sections"])
        obj = None
    elif response_protocol == "markdown":
        content, obj = raw, None
        if not content.strip():
            raise PageResponseError("page_empty_content", "The page content is empty")
    else:
        obj = parse_page(raw, strict=response_protocol == "summary-page-json")
        content = obj["content"]
    if figure_context is not None and obj is not None:
        from ultrakb.agent.page_figures import validate_ranges

        validate_ranges(obj, figure_context.ids)
    if expected_coverage:
        covered = obj.get("covered_ranges") if obj else None
        if (
            not isinstance(covered, list)
            or any(not isinstance(v, str) for v in covered)
            or len(covered) != len(set(covered))
            or set(covered) != set(expected_coverage)
        ):
            raise PageResponseError(
                "page_incomplete_coverage",
                "covered_ranges must acknowledge every supplied batch range exactly once",
            )
    if page_update is None and obj and obj.get("status") == "unchanged":
        raise PageResponseError(
            "page_unchanged_not_allowed",
            "New pages and summaries require knowledge content",
        )
    return raw


def confirmed_entity_type(delivered: dict, allowed_types: frozenset[str]) -> str:
    """A proposed classification needs confirmation in validated generated content."""
    value = delivered.get("type")
    return value if value in allowed_types else "other"
