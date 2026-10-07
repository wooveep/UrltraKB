"""Explicitly reassess retained originals without rewriting annotations or published knowledge."""

import uuid
from pathlib import Path

from openkb.application.version_review import (
    MetadataCorrection,
    VersionReview,
    _admission,
    review_source_version,
)
from openkb.knowledge_scope import KnowledgeScope
from openkb.locks import kb_ingest_lock
from openkb.mutation import mutation_scope
from openkb.source_catalog import record_path, write_record
from openkb.version_metadata import assess_version
from openkb.view_records import SourceMetadata


def reevaluate_source_version(
    kb_dir: Path, source_id: str, *, scope: KnowledgeScope | None = None
) -> VersionReview:
    """Refresh review candidates; user fields win and compilation still requires resume."""
    with kb_ingest_lock(kb_dir / ".openkb"):
        review = review_source_version(kb_dir, source_id, scope=scope)
        if review.status in {"cancelled", "superseded"}:
            raise ValueError("Cannot reevaluate a cancelled or superseded version review")
        admission = _admission(kb_dir, review)
        supplied = SourceMetadata.model_validate(
            {field: getattr(review.metadata, field) for field in review.user_fields}
        )
        assessment = assess_version(kb_dir, admission, supplied, reevaluate=True)
        missing = tuple(
            sorted(
                set(assessment.conflicts)
                | {
                    field
                    for field in ("product", "applicable_versions")
                    if not getattr(assessment.metadata, field)
                }
            )
        )
        if review.metadata == assessment.metadata and review.candidates == assessment.candidates:
            return review
        correction = MetadataCorrection(
            correction_id=uuid.uuid4().hex,
            review_id=review.review_id,
            previous_correction_id=review.correction_id,
            metadata=assessment.metadata,
            user_fields=review.user_fields,
        )
        updated = review.model_copy(
            update={
                "metadata": assessment.metadata,
                "candidates": assessment.candidates,
                "missing_fields": missing,
                "status": "blocked" if missing else "ready",
                "correction_id": correction.correction_id,
                "reason": "Retained original reassessed; confirm hints/conflicts, then resume.",
            }
        )
        path = record_path(kb_dir, "version-reviews", review.review_id)
        audit = record_path(kb_dir, "metadata-corrections", correction.correction_id)
        with mutation_scope(kb_dir, [path, audit], operation="reevaluate-source-version"):
            write_record(audit, correction)
            write_record(path, updated)
        return updated
