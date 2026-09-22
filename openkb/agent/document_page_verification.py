"""Re-bind a document-page review after deterministic publication preparation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from openkb.agent.document_page_contracts import (
    DocumentPageCandidate,
    preserved_page_contribution,
    publication_candidate_identity,
)
from openkb.agent.document_page_evidence import page_evidence
from openkb.agent.document_plan import RangeValue
from openkb.processing import ProcessingIncomplete
from openkb.sources import content_id


def reverify_normalized_candidate(
    page: Any,
    reader: Any,
    source: Any,
    parsed: Any,
    checkpoints: Any,
    wiki: Any,
    settings: dict[str, Any],
    limits: Any,
    candidate: DocumentPageCandidate,
    content: str,
    *,
    bundle: Any = None,
    on_event: Any = lambda event: None,
    known_omissions: list[dict[str, Any]] | None = None,
    resolution_ranges: list[RangeValue] | None = None,
    pool: Any,
) -> DocumentPageCandidate:
    """Review an exact final candidate if link normalization changed its bytes."""

    if content == candidate.content:
        return candidate
    if candidate.quality != "verified":
        raise ProcessingIncomplete("document_review_unverified", "generation")

    # Import lazily: document_pages owns the ordinary request payload while
    # this module is used after publication preparation.
    from openkb.agent.document_page_review import review_candidate
    from openkb.agent.document_pages import _occurrence_fields, _page_fields

    evidence, occurrences = page_evidence(
        page,
        reader,
        source,
        parsed,
        resolution_ranges=resolution_ranges,
    )
    if tuple(item["id"] for item in occurrences) != candidate.occurrence_ids:
        raise ProcessingIncomplete("page_evidence_changed", "generation")
    path = wiki / f"{page.name}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    retained, opening, closing, retained_identity = preserved_page_contribution(
        existing, source.source_id
    )
    if retained_identity != candidate.retained_identity:
        raise ProcessingIncomplete("page_contribution_changed", "generation")
    preserved_contribution = {
        "identity": retained_identity,
        "content": retained if retained.strip() else "",
    }
    on_event(
        {
            "stage": "generation",
            "operation": "verification_normalized_candidate",
            "page": page.name,
        }
    )
    review = review_candidate(
        page,
        evidence,
        occurrences,
        content,
        settings=settings,
        limits=limits,
        checkpoints=checkpoints,
        bundle=bundle,
        pool=pool,
        known_omissions=known_omissions or [],
        on_event=on_event,
        retained_identity=retained_identity,
        preserved_contribution=preserved_contribution,
    )
    if review["verdict"] not in {"supported", "advisory"}:
        raise ProcessingIncomplete(f"document_review_{review['verdict']}", "generation")

    review_receipt = {
        "mode": settings.get("review_mode", "critical"),
        "candidate": content_id(content),
        "verdict": "advisory" if review["verdict"] == "advisory" else "supported",
        "reviews": [review],
    }
    candidate_key = checkpoints.identity(
        "document-page-candidate-v1",
        {
            "page": _page_fields(page),
            "occurrences": _occurrence_fields(occurrences),
            "candidate": content_id(content),
            "preserved_contribution": retained_identity,
        },
    )
    review_receipt = {
        **review_receipt,
        "candidate_recovery": candidate_key,
        "publication_identity": publication_candidate_identity(
            checkpoints,
            page,
            occurrences,
            content,
            retained_identity,
            settings=settings,
            known_omissions=known_omissions or [],
        ),
    }
    checkpoints.save_recovery(
        candidate_key,
        "draft",
        {
            "output": {
                "content": content,
                "page_key": page.key,
                "quality": candidate.quality,
                "candidate": content_id(content),
                "occurrence_ids": list(candidate.occurrence_ids),
                "review_receipt": review_receipt,
                "retained_identity": retained_identity,
            },
            "review_mode": review_receipt["mode"],
        },
    )
    return replace(
        candidate,
        content=content,
        review_receipt=review_receipt,
        recovery_key=candidate_key,
    )
