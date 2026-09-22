"""Publication-state inheritance for immutable DocumentPlan ledgers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from openkb.agent.document_plan import DocumentPlan, from_dict


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
                old.target,
                old.subject_ranges,
                old.necessary_context,
                old.state,
            )
            == (
                page.key,
                page.kind,
                page.type,
                page.name,
                page.target,
                page.subject_ranges,
                page.necessary_context,
                page.state,
            )
        ):
            page.quality = "published"
            page.review_receipt = deepcopy(old.review_receipt)
