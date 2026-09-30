"""Durable clarification releases workers; an explicit resume starts new work."""

from pathlib import Path
from typing import Literal

from pydantic import field_validator

from openkb.application.execution import ExecutionContext
from openkb.ingest_records import UnitPublication
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.source_catalog import (
    Admission,
    read_record,
    read_source,
    read_source_revision,
    record_path,
    write_record,
)
from openkb.source_records import DiscoveryIntent, Record, RecordId, ViewId
from openkb.view_records import SourceMetadata, VersionAnnotation, VersionCandidate


class SupersededVersionReview(ValueError):
    """The retained input is no longer the source's selected target."""


def _metadata_fields(names):
    allowed = set(SourceMetadata.model_fields) - {"schema_version"}
    if any(name not in allowed for name in names):
        raise ValueError("Unknown version metadata field")
    return names


class RelatedVersionSource(Record):
    source_id: RecordId
    name: str
    annotation_id: RecordId
    metadata: SourceMetadata
    evidence: dict[str, str]
    matching_fields: tuple[str, ...]
    removed: bool = False


class VersionReview(Record):
    review_id: RecordId
    source_id: RecordId
    source_revision_id: RecordId
    source_generation: int
    annotation_id: RecordId
    name: str = ""
    view_id: ViewId
    normalization_id: RecordId
    status: Literal["blocked", "ready", "cancelled", "completed", "superseded"] = "blocked"
    metadata: SourceMetadata
    related_annotations: tuple[RecordId, ...]
    related_sources: tuple[RelatedVersionSource, ...] = ()
    missing_fields: tuple[str, ...]
    reason: str
    correction_id: RecordId | None = None
    candidates: tuple[VersionCandidate, ...] = ()
    user_fields: tuple[str, ...] = ()

    _validate_fields = field_validator("user_fields", "missing_fields")(_metadata_fields)


class MetadataCorrection(Record):
    correction_id: RecordId
    review_id: RecordId
    previous_correction_id: RecordId | None
    metadata: SourceMetadata
    user_fields: tuple[str, ...]

    _validate_fields = field_validator("user_fields")(_metadata_fields)


def review_source_version(
    kb_dir: Path, source_id: str, *, scope: KnowledgeScope | None = None
) -> VersionReview:
    """Open an explicit metadata correction without altering published knowledge."""
    from openkb.normalization import retain_published_normalization
    from openkb.unit_publication import list_source_units

    with kb_ingest_lock(kb_dir / ".openkb"):
        source = read_source(kb_dir, source_id)
        if source.removed or source.annotation_id is None:
            raise ValueError("Source has no current version annotation")
        annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
        if (
            annotation.source_id != source_id
            or annotation.source_revision_id != source.target_revision_id
        ):
            raise ValueError("Source annotation does not describe the current input")
        if scope:
            scope = resolve_scope(kb_dir, scope, writable=True)
            if scope.view_id != annotation.view_id:
                raise ValueError("Source belongs to another live knowledge view")
        path = record_path(kb_dir, "version-reviews", source.target_revision_id)
        previous = read_version_review(kb_dir, source.target_revision_id) if path.exists() else None
        if previous and previous.status != "completed":
            return previous
        revision = read_source_revision(kb_dir, source.target_revision_id)
        intent = read_record(
            kb_dir, "discovery-intents", revision.discovery_intent_id, DiscoveryIntent
        )
        admission = Admission(source, revision, intent)
        unit = next(
            (item for item in list_source_units(kb_dir, source_id) if item.key == "body"), None
        )
        if unit is None:
            raise ValueError("Source has no retained normalized input")
        identity = retain_published_normalization(kb_dir, admission, unit, annotation.view_id)
        missing = tuple(
            name
            for name in ("product", "applicable_versions")
            if not getattr(annotation.metadata, name)
        )
        review = VersionReview(
            review_id=revision.source_revision_id,
            source_id=source_id,
            source_revision_id=revision.source_revision_id,
            source_generation=source.target_generation,
            annotation_id=source.annotation_id,
            name=source.name,
            view_id=annotation.view_id,
            normalization_id=identity,
            status="blocked" if missing else "ready",
            metadata=annotation.metadata,
            related_annotations=(),
            missing_fields=missing,
            reason=(
                "Confirm applicability, then explicitly compile the retained input "
                "into its selected view."
            ),
            correction_id=previous.correction_id if previous else None,
            candidates=annotation.candidates,
            user_fields=tuple(
                name for name, origin in annotation.evidence.items() if origin == "user"
            ),
        )
        with mutation_scope(kb_dir, [path], operation="review-source-version"):
            write_record(path, review)
        return review


def save_version_wait(kb_dir, admission, normalized, state, assessment) -> UnitPublication:
    review = VersionReview(
        review_id=admission.revision.source_revision_id,
        source_id=admission.source.source_id,
        source_revision_id=admission.revision.source_revision_id,
        source_generation=admission.source.target_generation,
        annotation_id=admission.source.annotation_id,
        name=admission.source.name,
        view_id=state.view_id,
        normalization_id=normalized.normalization_id,
        metadata=assessment.metadata,
        related_annotations=assessment.related,
        missing_fields=assessment.missing_fields,
        reason=assessment.reason,
        candidates=assessment.candidates,
        user_fields=tuple(name for name, origin in assessment.evidence.items() if origin == "user"),
    )
    blocked = state.model_copy(
        update={"status": "blocked", "stage": "version_metadata", "message": assessment.reason}
    )
    records = {
        record_path(kb_dir, "version-reviews", review.review_id): review,
        record_path(kb_dir, "publications", state.publication_id): blocked,
        record_path(kb_dir, "attempts", state.attempt_id): blocked,
    }
    with mutation_scope(kb_dir, list(records), operation="wait-for-version-metadata"):
        for path, value in records.items():
            write_record(path, value)
    return blocked


def read_version_review(
    kb_dir: Path, review_id: str, *, scope: KnowledgeScope | None = None
) -> VersionReview:
    if scope is not None:
        scope = resolve_scope(kb_dir, scope)
    with kb_read_lock(kb_dir / ".openkb"):
        review = read_record(kb_dir, "version-reviews", review_id, VersionReview)
        annotation = read_record(kb_dir, "annotations", review.annotation_id, VersionAnnotation)
        if (
            annotation.source_id != review.source_id
            or annotation.source_revision_id != review.source_revision_id
            or annotation.view_id != review.view_id
        ):
            raise ValueError("Clarification annotation belongs to another input or view")
        from openkb.normalization import NormalizedInput

        normalized = read_record(kb_dir, "normalizations", review.normalization_id, NormalizedInput)
        if normalized.source_revision_id != review.source_revision_id:
            raise ValueError("Clarification normalization belongs to another input")
        if scope and (scope.read_only or scope.view_id != review.view_id):
            raise ValueError("Clarification belongs to another live knowledge view")
        related = []
        for identity in review.related_annotations:
            annotation = read_record(kb_dir, "annotations", identity, VersionAnnotation)
            source = read_source(kb_dir, annotation.source_id)
            matching = tuple(
                field
                for field in ("product", "family")
                if getattr(annotation.metadata, field) is not None
                and (
                    getattr(annotation.metadata, field) == getattr(review.metadata, field)
                    or any(
                        candidate.field == field
                        and getattr(annotation.metadata, field) in candidate.values
                        for candidate in review.candidates
                    )
                )
            )
            if source.source_id == review.source_id:
                matching = ("source_identity", *matching)
            related.append(
                RelatedVersionSource(
                    source_id=source.source_id,
                    name=source.name,
                    annotation_id=identity,
                    metadata=annotation.metadata,
                    evidence=annotation.evidence,
                    matching_fields=matching,
                    removed=source.removed,
                )
            )
        review = review.model_copy(update={"related_sources": tuple(related)})
        if review.status in {"blocked", "ready"}:
            try:
                _admission(kb_dir, review)
            except SupersededVersionReview as exc:
                return review.model_copy(update={"status": "superseded", "reason": str(exc)})
        return review


def list_version_reviews(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[VersionReview, ...]:
    if scope is not None:
        scope = resolve_scope(kb_dir, scope)
        if scope.read_only:
            return ()
    with kb_read_lock(kb_dir / ".openkb"):
        items = (
            read_version_review(kb_dir, path.stem)
            for path in sorted((kb_dir / ".openkb/catalog/version-reviews").glob("*.json"))
        )
        return tuple(
            item
            for item in items
            if item.status in {"blocked", "ready"}
            and (scope is None or scope.view_id == item.view_id)
        )


def _admission(kb_dir: Path, review: VersionReview) -> Admission:
    source = read_source(kb_dir, review.source_id)
    bound = (
        read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
        if source.annotation_id
        else None
    )
    resumed_binding = (
        review.status in {"ready", "completed"}
        and review.correction_id is not None
        and bound is not None
        and bound.source_id == source.source_id
        and bound.source_revision_id == review.source_revision_id
        and bound.metadata == review.metadata
    )
    if (
        source.removed
        or source.target_revision_id != review.source_revision_id
        or (source.target_generation != review.source_generation and not resumed_binding)
    ):
        raise SupersededVersionReview(
            "Clarification was superseded by another source revision or annotation"
        )
    revision = read_source_revision(kb_dir, review.source_revision_id)
    if revision.source_id != source.source_id:
        raise ValueError("Clarification belongs to another source")
    intent = read_record(kb_dir, "discovery-intents", revision.discovery_intent_id, DiscoveryIntent)
    return Admission(source, revision, intent)


def supplement_version_reviews(
    kb_dir: Path, updates: dict[str, SourceMetadata], *, scope: KnowledgeScope | None = None
) -> tuple[VersionReview, ...]:
    import uuid

    from openkb.version_metadata import assess_version

    changed = []
    records = {}
    with kb_ingest_lock(kb_dir / ".openkb"):
        for identity, patch in updates.items():
            review = read_version_review(kb_dir, identity, scope=scope)
            if review.status not in {"blocked", "ready"}:
                raise ValueError("Only a pending clarification can be supplemented")
            admission = _admission(kb_dir, review)
            metadata = SourceMetadata.model_validate(
                {**review.metadata.model_dump(), **patch.model_dump(exclude_unset=True)}
            )
            user_fields = tuple(
                sorted(set(review.user_fields) | (patch.model_fields_set - {"schema_version"}))
            )
            user_metadata = SourceMetadata.model_validate(
                {name: getattr(metadata, name) for name in user_fields}
            )
            assessment = assess_version(
                kb_dir, admission, user_metadata, candidates=review.candidates
            )
            correction = MetadataCorrection(
                correction_id=uuid.uuid4().hex,
                review_id=identity,
                previous_correction_id=review.correction_id,
                metadata=metadata,
                user_fields=user_fields,
            )
            missing = tuple(
                sorted(
                    {
                        field
                        for field in ("product", "applicable_versions")
                        if not getattr(metadata, field)
                    }
                    | set(assessment.conflicts)
                )
            )
            review = review.model_copy(
                update={
                    "metadata": metadata,
                    "source_generation": admission.source.target_generation,
                    "correction_id": correction.correction_id,
                    "user_fields": user_fields,
                    "missing_fields": missing,
                    "reason": assessment.reason if missing else "",
                    "status": "blocked" if missing else "ready",
                }
            )
            path = record_path(kb_dir, "version-reviews", identity)
            correction_path = record_path(kb_dir, "metadata-corrections", correction.correction_id)
            records[correction_path] = correction
            records[path] = review
            changed.append(review)
        with mutation_scope(kb_dir, list(records), operation="supplement-version-metadata"):
            for path, record in records.items():
                write_record(path, record)
    return tuple(changed)


def resume_version_review(
    kb_dir: Path,
    review_id: str,
    *,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> IngestResult:
    from dataclasses import replace

    from openkb.application.ingestion import import_prepared_source
    from openkb.compilation_report import collect_compile_report
    from openkb.inputs import prepared_input
    from openkb.lifecycle import read_lifecycle

    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()

    with (
        read_lifecycle(kb_dir, cancelled=context.cancelled, on_wait=context.waiting),
        kb_ingest_lock(
            kb_dir / ".openkb",
            cancelled=context.cancelled if context else None,
            on_wait=context.waiting if context else None,
        ),
    ):
        review = read_version_review(kb_dir, review_id, scope=scope)
        if review.status not in {"ready", "completed"}:
            raise ValueError("Clarification must be ready before an explicit resume")
        admission = _admission(kb_dir, review)
        with (
            prepared_input(kb_dir / admission.revision.original) as prepared,
            context.begin(kb_dir) as credentials,
            collect_compile_report() as report,
        ):
            metadata = SourceMetadata.model_validate(
                {name: getattr(review.metadata, name) for name in review.user_fields}
            )
            result = import_prepared_source(
                kb_dir,
                prepared,
                context=context,
                metadata=metadata,
                admission=admission,
                bundle=credentials,
                on_event=context.on_event,
                retry_confirmed=True,
            )
            return replace(
                result,
                quality=tuple(report.quality),
                unfinished=result.unfinished + tuple(report.unfinished),
            )


def complete_version_review(
    kb_dir: Path, review: VersionReview, admission: Admission, state: UnitPublication
) -> None:
    if state.status not in {"completed", "empty", "retired", "awaiting_confirmation"}:
        return
    path = record_path(kb_dir, "version-reviews", review.review_id)
    with mutation_scope(kb_dir, [path], operation="complete-version-review"):
        write_record(
            path,
            review.model_copy(
                update={
                    "status": "completed",
                    "source_generation": admission.source.target_generation,
                    "view_id": state.view_id,
                    "annotation_id": admission.source.annotation_id,
                }
            ),
        )


def cancel_version_review(
    kb_dir: Path, review_id: str, *, scope: KnowledgeScope | None = None
) -> VersionReview:
    from openkb.unit_publication import list_source_units, read_unit_publication

    with kb_ingest_lock(kb_dir / ".openkb"):
        review = read_version_review(kb_dir, review_id, scope=scope)
        if review.status == "cancelled":
            return review
        if review.status not in {"blocked", "ready"}:
            raise ValueError("Only a pending clarification can be cancelled")
        admission = _admission(kb_dir, review)
        review = review.model_copy(
            update={"status": "cancelled", "reason": "Version clarification cancelled"}
        )
        intent = admission.discovery_intent.model_copy(update={"cancelled": True})
        records: dict[Path, Record] = {
            record_path(kb_dir, "version-reviews", review_id): review,
            record_path(kb_dir, "discovery-intents", intent.intent_id): intent,
        }
        for unit in list_source_units(kb_dir, review.source_id):
            state = read_unit_publication(kb_dir, unit.unit_id, review.view_id)
            if state.status != "blocked" or state.stage != "version_metadata":
                continue
            stopped = state.model_copy(update={"status": "stopped", "message": review.reason})
            records[record_path(kb_dir, "publications", state.publication_id)] = stopped
            records[record_path(kb_dir, "attempts", state.attempt_id)] = stopped
        with mutation_scope(kb_dir, list(records), operation="cancel-version-clarification"):
            for path, value in records.items():
                write_record(path, value)
        return review
