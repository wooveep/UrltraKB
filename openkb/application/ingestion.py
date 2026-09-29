"""Frozen-source import: admission survives each independently published unit."""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Callable, Literal

from openkb.application.execution import ExecutionContext
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.ingest_records import UnitPublication, UnitRevision
from openkb.ingest_result import ImportUnitOutcome, IngestResult
from openkb.inputs import PreparedInput
from openkb.knowledge_scope import KnowledgeScope
from openkb.locks import LockCancelled
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.source_catalog import (
    Admission,
    admit_source_revision,
    read_record,
    record_path,
)
from openkb.unit_publication import (
    begin_unit_attempt,
    plan_import_units,
    prepare_compile_view,
    publish_unit_revision,
    record_unit_failure,
)
from openkb.view_records import SourceMetadata

logger = logging.getLogger(__name__)


def result_from_publication(
    kb_dir: Path,
    admission: Admission,
    state: UnitPublication,
    *,
    status: Literal["added", "skipped", "failed", "blocked", "partial", "stopped"],
) -> IngestResult:
    resources = [str(kb_dir / admission.revision.original)]
    from openkb.application.sources import processing_details
    from openkb.inputs import input_version

    actual_source = None
    manifest = None
    if state.successful_revision_id:
        actual = read_record(kb_dir, "unit-revisions", state.successful_revision_id, UnitRevision)
        actual_source = actual.source_revision_id
    if state.knowledge_revision_id:
        from openkb.application.sources import _manifest

        published = _manifest(kb_dir, state)
        manifest = published[1] if published else None
        wiki = (
            kb_dir
            / ".openkb/knowledge"
            / state.view_id
            / "revisions"
            / state.knowledge_revision_id
            / "wiki"
        )
        resources.extend(
            str(path)
            for path in [wiki / "summaries" / f"{admission.source.doc_name}.md"]
            if path.is_file()
        )
    outcome = ImportUnitOutcome(
        state.unit_id,
        state.status,
        state.target_revision_id,
        state.successful_revision_id,
        actual_source,
        state.knowledge_revision_id,
        state.view_id,
        state.proposal_id,
        state.error_type,
        state.message,
        state.job_id,
        **processing_details(kb_dir, state, manifest),
    )
    return IngestResult(
        admission.source.identity,
        status,
        tuple(resources),
        unfinished=() if status in {"added", "skipped"} else (state.stage,),
        input_version=input_version(
            admission.revision.digest,
            {asset.original_reference: asset.digest for asset in admission.revision.assets},
        ),
        source_id=admission.source.source_id,
        source_revision_id=admission.revision.source_revision_id,
        units=(outcome,),
        discovery_pending=int(admission.discovery_intent.status == "pending"),
        message=state.message,
    )


def import_prepared_source(
    kb_dir: Path,
    prepared: PreparedInput,
    *,
    bundle=None,
    context: ExecutionContext | None = None,
    on_event: Callable[[dict], None] | None = None,
    origin_url: str | None = None,
    report=logger.info,
    metadata: SourceMetadata | None = None,
    scope: KnowledgeScope | None = None,
    admission: Admission | None = None,
    retry_confirmed: bool = False,
) -> IngestResult:
    """Caller owns the real KB write lease and frozen input for the whole call."""
    from openkb.agent.compiler import (
        DEFAULT_COMPILE_CONCURRENCY,
        compile_long_doc,
        compile_short_doc,
    )
    from openkb.application.documents import _run_compile_with_retry
    from openkb.indexer import index_long_document

    check_stop = context.check_stop if context else lambda: None
    admission = admission or admit_source_revision(
        kb_dir, prepared, identity=origin_url, check_stop=check_stop
    )
    if prepared.digest != admission.revision.digest:
        raise ValueError("Retained input no longer matches the source revision")
    from openkb.application.version_review import (
        VersionReview,
        complete_version_review,
        save_version_wait,
    )
    from openkb.application.views import bind_source_view
    from openkb.normalization import (
        normalization_fingerprint,
        read_normalization,
        restore_normalization,
        retain_normalization,
    )
    from openkb.version_metadata import assess_version

    review_path = record_path(kb_dir, "version-reviews", admission.revision.source_revision_id)
    review = (
        read_record(kb_dir, "version-reviews", admission.revision.source_revision_id, VersionReview)
        if review_path.exists()
        else None
    )
    if review and review.status == "cancelled":
        return IngestResult(
            admission.source.identity,
            "blocked",
            (str(kb_dir / admission.revision.original),),
            source_id=admission.source.source_id,
            source_revision_id=admission.revision.source_revision_id,
            unfinished=("version_metadata",),
            message="Version clarification was cancelled; no work resumed.",
        )
    assessment = assess_version(
        kb_dir, admission, metadata, scope=scope, candidates=review.candidates if review else None
    )
    admission, scope = bind_source_view(
        kb_dir,
        admission,
        assessment.metadata,
        scope=scope,
        evidence=assessment.evidence,
        candidates=assessment.candidates,
    )
    fingerprint = (
        read_normalization(kb_dir, review.normalization_id)[1].fingerprint
        if review
        else normalization_fingerprint(
            kb_dir,
            scope=scope,
            bundle=bundle,
            source_revision=admission.revision,
            doc_name=admission.source.doc_name,
        )
    )

    unit, revision = plan_import_units(kb_dir, admission, fingerprint)
    state, runnable = begin_unit_attempt(
        kb_dir,
        unit,
        revision,
        discovery_intent=admission.discovery_intent,
        view_id=scope.view_id,
        retry_confirmed=retry_confirmed,
    )
    if not runnable:
        if review:
            complete_version_review(kb_dir, review, admission, state)
        return result_from_publication(
            kb_dir, admission, state, status="skipped" if state.status == "completed" else "blocked"
        )
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    concurrency = resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY
    stage = "conversion"
    try:
        if on_event:
            on_event({"stage": "converting", "source": admission.source.name})
        directory, normalized = retain_normalization(
            kb_dir,
            admission,
            prepared,
            fingerprint,
            check_stop=check_stop,
            scope=scope,
            bundle=bundle,
        )
        if assessment.missing_fields:
            state = save_version_wait(kb_dir, admission, normalized, state, assessment)
            return result_from_publication(kb_dir, admission, state, status="blocked")
        with prepare_compile_view(kb_dir, scope.view_id) as view:
            working = view.scope.wiki_dir.parent
            check_stop()
            with mutation_scope(kb_dir, [working], operation="compile-import-unit"):
                converted = restore_normalization(directory, normalized, working)
                index_ref = None
                if converted.is_long_doc:
                    stage = "indexing"
                    if admission.revision.source_format in {"md", "markdown"}:
                        raise ValueError(
                            "Segmented Markdown processing is not available yet; "
                            "complete input retained"
                        )
                    if converted.raw_path is None:
                        raise ValueError("Normalized PDF is missing")
                    indexed = index_long_document(
                        converted.raw_path,
                        kb_dir,
                        doc_name=unit.doc_name,
                        scope=view.scope,
                        storage_path=working / "index",
                    )
                    index_ref = indexed.doc_id
                    compile_document = partial(
                        compile_long_doc,
                        unit.doc_name,
                        view.scope.wiki_dir / "summaries" / f"{unit.doc_name}.md",
                        indexed.doc_id,
                        kb_dir,
                        model,
                        doc_description=indexed.description,
                        max_concurrency=concurrency,
                        bundle=bundle,
                        scope=view.scope,
                    )
                else:
                    if converted.source_path is None:
                        raise ValueError("Normalized source text is missing")
                    compile_document = partial(
                        compile_short_doc,
                        unit.doc_name,
                        converted.source_path,
                        kb_dir,
                        model,
                        max_concurrency=concurrency,
                        bundle=bundle,
                        scope=view.scope,
                    )
                stage = "compilation"
                if on_event:
                    on_event({"stage": "compiling", "source": admission.source.name})
                _run_compile_with_retry(compile_document, "Compiling wiki", report=report)
                check_stop()
            stage = "publication"
            extension = "json" if converted.is_long_doc else "md"
            from openkb.source_map import freeze_pdf_map

            page_map = view.scope.wiki_dir / "sources" / f"{unit.doc_name}.json"
            # A pre-page-map publication can be clarified using its retained
            # conversion. It must not gain invented coverage or be reconverted.
            source_map = (
                freeze_pdf_map(
                    view.scope.wiki_dir, unit.doc_name, kb_dir / admission.revision.original
                )
                if admission.revision.source_format == "pdf"
                and (converted.processing is not None or page_map.exists())
                else None
            )
            if admission.revision.source_format in {"md", "markdown"}:
                from openkb.source_map import freeze_text_map

                source_map = freeze_text_map(view.scope.wiki_dir, unit.doc_name)
            state = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.{extension}",
                source_map=source_map,
                processing=converted.processing,
                normalized_format="pdf" if converted.is_long_doc else "markdown",
                is_long=converted.is_long_doc,
                index_ref=index_ref,
                check_stop=check_stop,
            )
    except RecoveryRequired:
        raise
    except LockCancelled as exc:
        record_unit_failure(kb_dir, state, stage, exc, discovery_intent=admission.discovery_intent)
        raise
    except Exception as exc:
        logger.debug("Unit processing failed", exc_info=True)
        failed = record_unit_failure(kb_dir, state, stage, exc)
        return result_from_publication(kb_dir, admission, failed, status="failed")
    # Receipt/notification failure must not downgrade a committed business result.
    if on_event:
        try:
            on_event(
                {
                    "stage": "committed",
                    "source_id": admission.source.source_id,
                    "source_revision_id": admission.revision.source_revision_id,
                }
            )
        except Exception:
            logger.warning("Import committed but its progress notification was not delivered")
    if review and state.status in {"completed", "awaiting_confirmation"}:
        try:
            complete_version_review(kb_dir, review, admission, state)
        except RecoveryRequired:
            raise
        except Exception:
            logger.warning("Knowledge published; clarification completion will reconcile on retry")
    return result_from_publication(
        kb_dir, admission, state, status="added" if state.status == "completed" else "blocked"
    )
