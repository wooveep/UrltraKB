"""Publication-state inheritance for immutable DocumentPlan ledgers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from openkb.agent.document_plan import DocumentPlan, from_dict
from openkb.sources import content_id


def suggestion_identity(page: Any) -> str:
    value = page.to_dict()
    for key in ("quality", "review_receipt", "state"):
        value.pop(key, None)
    return content_id(value)


def remember_preparation(plan: DocumentPlan, suggestion: Any) -> None:
    from openkb.agent.document_page_resolution import preparation_rules

    plan.metadata["preparation_rules"] = preparation_rules()
    plan.metadata.setdefault("prepared_suggestions", {})[suggestion.key] = suggestion_identity(
        suggestion
    )


def inherit_publication_state(final: DocumentPlan, saved: Any) -> None:
    """Carry a completed formal receipt across terminal-plan reconstruction.

    The planning ledger intentionally contains only source-plan state.  A
    Continue can materialize that same accepted ledger before it creates a
    successor proposal, so overwriting the sole plan recovery record must not
    erase the completed proposal receipt which is still needed by publication
    repair.  Stable already-published pages are retained as completed work;
    pending/omitted pages remain available for a new generation attempt.
    """

    try:
        previous = from_dict(saved)
    except (AttributeError, KeyError, TypeError, ValueError):
        return
    if previous.metadata.get("recovery_key") != final.metadata.get("recovery_key"):
        return
    if previous.metadata.get("protocol") != final.metadata.get("protocol"):
        return
    previous_pages = {page.key: page for page in previous.pages}
    if final.metadata.get("protocol") == "document-plan-v4":
        from openkb.agent.document_page_resolution import preparation_rules

        if previous.metadata.get("preparation_rules") != preparation_rules():
            return
        final.metadata["preparation_rules"] = preparation_rules()
        receipts = previous.metadata.get("prepared_suggestions", {})
        for page in final.pages:
            old = previous_pages.get(page.key)
            if old is not None and receipts.get(page.key) == suggestion_identity(page):
                for field in (
                    "subject_ranges",
                    "context_ranges",
                    "scope_resolution",
                    "evidence_scope",
                    "planning_notes",
                    "location_hints",
                    "state",
                ):
                    setattr(page, field, deepcopy(getattr(old, field)))
                final.metadata.setdefault("prepared_suggestions", {})[page.key] = receipts[page.key]
    for field in ("publication_receipt", "publication_pending", "publication_page_receipts"):
        value = previous.metadata.get(field)
        if isinstance(value, dict):
            final.metadata[field] = deepcopy(value)

    receipt = previous.metadata.get("publication_receipt")
    paths = receipt.get("pages") if isinstance(receipt, dict) else None
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return
    published_paths = set(paths)
    previous_pages = {page.key: page for page in previous.pages}

    def references(plan: DocumentPlan, key: str) -> list[dict[str, Any]]:
        return [row.to_dict() for row in plan.external_references if key in row.affected_pages]

    for page in final.pages:
        old = previous_pages.get(page.key)
        path = page.target or page.name
        if not path.startswith(("concepts/", "entities/")):
            path = f"{page.kind}s/{path}"
        if (
            old is not None
            and old.quality == "published"
            and path + ".md" in published_paths
            and (
                old.key,
                old.kind,
                old.type,
                old.name,
                old.title,
                old.purpose,
                old.target,
                old.subject_ranges,
                old.context_ranges,
                old.planning_notes,
                old.location_hints,
                old.scope_resolution,
                old.evidence_scope,
                old.necessary_context,
                old.limitations,
                references(previous, old.key),
                old.state,
            )
            == (
                page.key,
                page.kind,
                page.type,
                page.name,
                page.title,
                page.purpose,
                page.target,
                page.subject_ranges,
                page.context_ranges,
                page.planning_notes,
                page.location_hints,
                page.scope_resolution,
                page.evidence_scope,
                page.necessary_context,
                page.limitations,
                references(final, page.key),
                page.state,
            )
        ):
            page.quality = "published"
            page.review_receipt = deepcopy(old.review_receipt)
