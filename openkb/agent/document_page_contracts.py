"""Private candidate and preserved-page contracts for document generation."""

from dataclasses import dataclass
from typing import Any

from openkb.agent.document_page_receipts import valid_critical_review_receipt
from openkb.agent.document_plan import PagePlan
from openkb.agent.evidence_pages import _previous_contribution
from openkb.implementation import module_revision
from openkb.sources import content_id


@dataclass(frozen=True)
class DocumentPageCandidate:
    """A private candidate; ``quality`` is never publication permission by itself."""

    content: str
    quality: str
    occurrence_ids: tuple[str, ...]
    review_receipt: dict[str, Any]
    retained_identity: str
    recovery_key: str = ""


@dataclass(frozen=True)
class DocumentPageCandidateReference:
    """A body-free pointer to one durably saved private page candidate."""

    page_key: str
    recovery_key: str
    candidate: str
    quality: str
    occurrence_ids: tuple[str, ...]
    retained_identity: str


def _page_input(page: PagePlan) -> dict[str, Any]:
    """Project page fields that actually enter generation and review requests."""

    return {
        "key": page.key,
        "kind": page.kind,
        "type": page.type,
        "name": page.name,
        "title": page.title,
        "purpose": page.purpose,
        "target": page.target,
        "subject_ranges": page.subject_ranges,
        "necessary_context": page.necessary_context,
    }


def publication_candidate_identity(
    checkpoints: Any,
    page: PagePlan,
    occurrences: list[dict[str, Any]],
    content: str,
    retained_identity: str,
    *,
    settings: dict[str, Any],
    known_omissions: list[dict[str, Any]],
) -> str:
    """Bind a reusable published candidate to every material private input.

    A recovery draft's own key intentionally has compatibility behavior (for
    example a none-mode draft can later be critically reviewed).  Publication
    reuse is stricter: it is allowed only when the exact page/evidence,
    rendering context, and review profile still match this run.
    """

    return checkpoints.identity(
        "document-page-publication-v1",
        {
            "page": _page_input(page),
            "occurrences": [
                {
                    "id": occurrence.get("id"),
                    "reference": occurrence.get("reference"),
                    "routes": occurrence.get("routes"),
                }
                for occurrence in occurrences
            ],
            "candidate": content_id(content),
            "preserved_contribution": retained_identity,
            # Targets/assets only govern local normalization. Their effect is
            # already bound by the final candidate digest; they are not sent
            # to the model and an unrelated new wiki target must not force a
            # faithful published contribution to regenerate.
            "generation": {
                "known_omissions": known_omissions,
                "schema": settings.get("_document_schema", ""),
                "language": settings.get("language"),
            },
            "review": {
                "mode": settings.get("review_mode", "critical"),
                "request_policy": checkpoints.analysis_options,
                "verification_options": checkpoints.verification_options,
                "adjudication_options": checkpoints.adjudication_options,
            },
            "implementation": {
                name: module_revision("openkb.agent." + name)
                for name in (
                    "document_pages",
                    "document_page_contracts",
                    "document_page_evidence",
                    "document_page_review",
                    "document_page_verification",
                    "evidence_markup",
                )
            },
        },
    )


def reference_document_page_candidate(
    candidate: DocumentPageCandidate, *, page_key: str
) -> DocumentPageCandidateReference:
    """Discard a generated body after its immutable recovery record is durable."""

    if not isinstance(page_key, str) or not page_key or not candidate.recovery_key:
        raise ValueError("Document page candidate has no durable recovery identity")
    return DocumentPageCandidateReference(
        page_key=page_key,
        recovery_key=candidate.recovery_key,
        candidate=content_id(candidate.content),
        quality=candidate.quality,
        occurrence_ids=candidate.occurrence_ids,
        retained_identity=candidate.retained_identity,
    )


def restore_document_page_candidate(
    checkpoints: Any, reference: DocumentPageCandidateReference
) -> DocumentPageCandidate:
    """Reload one private body on demand and re-check its compact pointer."""

    record = checkpoints.load_recovery(reference.recovery_key, "draft")
    output = record.get("output") if isinstance(record, dict) else None
    if not isinstance(output, dict):
        raise ValueError("Document page candidate recovery is missing")
    content = output.get("content")
    occurrences = output.get("occurrence_ids")
    receipt = output.get("review_receipt")
    if (
        not isinstance(content, str)
        or output.get("page_key") != reference.page_key
        or output.get("quality") != reference.quality
        or output.get("candidate") != reference.candidate
        or content_id(content) != reference.candidate
        or not isinstance(occurrences, list)
        or tuple(occurrences) != reference.occurrence_ids
        or not all(isinstance(item, str) for item in occurrences)
        or not isinstance(receipt, dict)
        or output.get("retained_identity") != reference.retained_identity
    ):
        raise ValueError("Document page candidate recovery is invalid")
    return DocumentPageCandidate(
        content=content,
        quality=reference.quality,
        occurrence_ids=reference.occurrence_ids,
        review_receipt=receipt,
        retained_identity=reference.retained_identity,
        recovery_key=reference.recovery_key,
    )


def _require_critical_review_checkpoint(
    checkpoints: Any,
    page: PagePlan,
    candidate: str,
    retained_identity: str,
    receipt: dict[str, Any],
) -> None:
    """Verify that a page receipt points to its exact immutable review result."""

    if not valid_critical_review_receipt(receipt, candidate):
        raise ValueError("Published page has no valid critical review receipt")
    review = receipt["reviews"][0]
    try:
        record = checkpoints.record(review["checkpoint"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Published page review checkpoint is unavailable") from exc
    if not isinstance(record, dict):
        raise ValueError("Published page review checkpoint is unavailable")
    contract = record.get("contract") if isinstance(record, dict) else None
    payload = contract.get("payload") if isinstance(contract, dict) else None
    dependencies = contract.get("dependencies") if isinstance(contract, dict) else None
    value = record.get("value") if isinstance(record, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("stage") != "verification"
        or payload.get("page") != _page_input(page)
        or payload.get("review_mode") != "critical"
        or not isinstance(payload.get("candidate"), dict)
        or payload["candidate"].get("full_content_digest") != candidate
        or not isinstance(payload.get("preserved_contribution"), dict)
        or payload["preserved_contribution"].get("identity") != retained_identity
        or not isinstance(dependencies, dict)
        or dependencies.get("page") != _page_input(page)
        or dependencies.get("candidate") != candidate
        or dependencies.get("preserved_contribution") != retained_identity
        or dependencies.get("mode") not in {"critical", "critical_adjudication"}
        or not isinstance(value, dict)
        or value.get("verdict") != review["verdict"]
        or value.get("reason") != review["reason"]
        or value.get("issues", []) != review["issues"]
        or content_id(value) != review["result"]
        or record.get("dispatch_output_tokens") != review["dispatch_output_tokens"]
    ):
        raise ValueError("Published page review checkpoint is invalid")


def restore_published_document_page_candidate(
    checkpoints: Any, page: PagePlan
) -> DocumentPageCandidate:
    """Restore a formerly published candidate from its page-level receipt."""

    receipt = page.review_receipt
    if not isinstance(receipt, dict):
        raise ValueError("Published page has no review receipt")
    recovery_key = receipt.get("candidate_recovery")
    candidate_digest = receipt.get("candidate")
    if not isinstance(recovery_key, str) or not isinstance(candidate_digest, str):
        raise ValueError("Published page has no candidate recovery receipt")
    record = checkpoints.load_recovery(recovery_key, "draft")
    output = record.get("output") if isinstance(record, dict) else None
    if not isinstance(output, dict):
        raise ValueError("Published page candidate recovery is missing")
    content = output.get("content")
    occurrences = output.get("occurrence_ids")
    retained_identity = output.get("retained_identity")
    if (
        not isinstance(content, str)
        or output.get("page_key") != page.key
        or output.get("quality") != "verified"
        or output.get("candidate") != candidate_digest
        or content_id(content) != candidate_digest
        or not isinstance(occurrences, list)
        or not all(isinstance(item, str) for item in occurrences)
        or not isinstance(retained_identity, str)
        or output.get("review_receipt") != receipt
    ):
        raise ValueError("Published page candidate recovery is invalid")
    _require_critical_review_checkpoint(
        checkpoints,
        page,
        candidate_digest,
        retained_identity,
        receipt,
    )
    return DocumentPageCandidate(
        content=content,
        quality="verified",
        occurrence_ids=tuple(occurrences),
        review_receipt=receipt,
        retained_identity=retained_identity,
        recovery_key=recovery_key,
    )


def page_retained_identity(wiki: Any, page: PagePlan, source_id: str) -> str:
    """Fingerprint the non-current-source contribution that must be preserved."""

    path = wiki / f"{page.name}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    return preserved_page_contribution(existing, source_id)[3]


def preserved_page_contribution(existing: str, source_id: str) -> tuple[str, str, str, str]:
    """Return preserved page material and its stable identity.

    The contribution splitter can leave a formatting-only newline after this
    source's own marker.  That newline is retained when a page is rendered,
    but it must not make the non-current contribution look different on a
    subsequent resume.
    """

    retained, opening, closing = _previous_contribution(existing, source_id)
    identity_retained = retained if retained.strip() else ""
    return retained, opening, closing, content_id((identity_retained, opening, closing))
