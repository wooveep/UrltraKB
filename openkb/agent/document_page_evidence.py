"""Exact original-evidence reads for one formal document page."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from openkb.agent.document_plan import PagePlan, RangeValue, range_intervals
from openkb.evidence import Evidence
from openkb.processing import ProcessingIncomplete, processing_checkpoint


def page_resolution_ranges(plan: Any, page: PagePlan) -> list[RangeValue]:
    """Return program-owned evidence that resolved an issue affecting ``page``.

    A resolution is not model-authored page context.  It is a separately typed
    formal-plan receipt, whose exact source ranges become readable only for
    pages explicitly named by the resolved issue.  Keeping this derivation
    here prevents an issue's free-text explanation from becoming generation
    or review input.
    """

    unresolved = {item.key: item for item in plan.unresolved}
    result: list[RangeValue] = []
    for resolution in plan.resolutions:
        issue = unresolved.get(resolution.unresolved_key)
        if issue is None or issue.status != "resolved":
            raise ValueError("Resolution has no resolved formal-plan issue")
        if page.key in issue.affected_pages:
            result.extend(resolution.basis_ranges)
    return result


def page_occurrence_descriptors(
    page: PagePlan,
    source: Any,
    parsed: Any,
    *,
    resolution_ranges: list[RangeValue] | None = None,
) -> list[dict[str, Any]]:
    """Describe exact body/context/resolution ranges without source reads."""

    by_index: dict[int, list[tuple[int, int, dict[str, str]]]] = {}

    def add_ranges(ranges: list[RangeValue], route: str, relation: str | None = None) -> None:
        for value in ranges:
            for index, start, end in range_intervals(value, parsed, f"planned page {page.key}"):
                block = parsed.blocks[index]
                if "attachment" in getattr(block, "location", {}):
                    # Attachments remain retained source assets. Their contents are
                    # not silently treated as read by document planning or generation.
                    continue
                item = {"route": route}
                if relation:
                    item["relation"] = relation
                by_index.setdefault(index, []).append((start, end, item))

    add_ranges(page.subject_ranges, "page_body")
    for context in page.necessary_context:
        if not isinstance(context, dict):
            raise ValueError(f"Invalid necessary context on planned page {page.key}")
        add_ranges(context.get("ranges", []), "context_only", context.get("relation"))
        # A basis is provenance for the same context relationship, not a
        # separate semantic role exposed to generation or review.
        add_ranges(context.get("basis_ranges", []), "context_only", context.get("relation"))
    add_ranges(resolution_ranges or [], "resolution_evidence")

    result: list[dict[str, Any]] = []
    ordinal = 0
    for index in sorted(by_index):
        entries = by_index[index]
        boundaries = sorted({point for start, end, _ in entries for point in (start, end)})
        block = parsed.blocks[index]
        for start, end in zip(boundaries, boundaries[1:]):
            routes = []
            for left, right, route in entries:
                if left <= start and end <= right and route not in routes:
                    routes.append(route)
            if not routes:
                continue
            # Planning may split one oversized source block into adjacent exact
            # RangeRefs. They remain one logical occurrence when their route is
            # unchanged, so generation cannot silently publish fragments of one command.
            if (
                result
                and result[-1]["block_index"] == index
                and result[-1]["routes"] == routes
                and result[-1]["reference"]["end"] == start
            ):
                result[-1]["reference"]["end"] = end
                continue
            ordinal += 1
            result.append(
                {
                    "id": f"o{ordinal}",
                    "block_index": index,
                    "reference": asdict(
                        Evidence(source.source_id, source.id, parsed.id, block.id, start, end)
                    ),
                    "routes": routes,
                    "block": block,
                }
            )
    return result


def page_evidence(
    page: PagePlan,
    reader: Any,
    source: Any,
    parsed: Any,
    *,
    resolution_ranges: list[RangeValue] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read only planned body, context, and resolved-dependency evidence."""

    descriptors = page_occurrence_descriptors(
        page, source, parsed, resolution_ranges=resolution_ranges
    )
    occurrences: list[dict[str, Any]] = []
    for descriptor in descriptors:
        processing_checkpoint("generation")
        reference = Evidence(**descriptor["reference"])
        try:
            view = reader.read(reference, max_chars=reader.complete_bound(reference))
        except (OSError, ValueError, AttributeError) as exc:
            raise ProcessingIncomplete("planned_evidence_unavailable", "generation") from exc
        text = getattr(view, "text", "")
        if not isinstance(text, str) or not text:
            raise ProcessingIncomplete("planned_evidence_unavailable", "generation")
        occurrence = {
            "id": descriptor["id"],
            "reference": descriptor["reference"],
            "routes": descriptor["routes"],
            "text": text,
            "location": getattr(view, "location", {}),
            "kind": getattr(descriptor["block"], "kind", "paragraph"),
            # The page generator may authorize only assets that its exact
            # source occurrences read.  Retain the parser-bound identities on
            # each occurrence rather than handing every source asset to every
            # candidate page.
            "assets": list(getattr(descriptor["block"], "assets", ())),
        }
        context_data = getattr(view, "context_data", None)
        if context_data:
            # Structured context distinguishes original wording from parser
            # metadata; the legacy mixed-display string would compete with it.
            occurrence["context_data"] = context_data
        elif context := getattr(view, "context", None):
            occurrence["context"] = context
        occurrences.append(occurrence)
    if not occurrences:
        raise ProcessingIncomplete("planned_page_has_no_readable_evidence", "generation")
    return (
        {
            "group_id": f"document-page:{page.key}",
            "source_id": source.source_id,
            "version_id": source.id,
            "parse_id": parsed.id,
            "blocks": occurrences,
        },
        occurrences,
    )
