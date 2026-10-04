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
from openkb.inputs import TEXT_SOURCE_EXTENSIONS, PreparedInput
from openkb.knowledge_scope import KnowledgeScope
from openkb.llm_usage_execution import track_import_unit, track_import_usage
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
    from openkb.ingest_records import ImportUnit

    unit = read_record(kb_dir, "units", state.unit_id, ImportUnit)
    resources = [str(kb_dir / admission.revision.original)]
    from openkb.application.sources import processing_details
    from openkb.inputs import input_version
    from openkb.workbooks.catalog import unit_name

    actual_source = None
    manifest = None
    published = None
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
            str(path) for path in [wiki / "summaries" / f"{unit.doc_name}.md"] if path.is_file()
        )
    from openkb.conversion_artifacts import read_conversion_artifacts

    conversion = read_conversion_artifacts(
        kb_dir,
        state.successful_revision_id or state.target_revision_id,
        actual_source or admission.revision.source_revision_id,
    )
    if conversion:
        resources.append(str(kb_dir / conversion["internal_pdf_path"]))
    from openkb.source_metrics import unit_metrics

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
        key=unit.key,
        doc_name=unit.doc_name,
        name=unit_name(kb_dir, unit, state.target_revision_id, admission.source.name),
        **processing_details(kb_dir, state, manifest),
        **unit_metrics(kb_dir, state, published, unit.doc_name),
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
        **pending_counts(kb_dir, admission),
        message=state.message,
    )


def pending_counts(kb_dir, admission):
    """Count persisted work even when body inventory or version clarification fails."""
    from openkb.pending.store import jobs

    pending_jobs = [
        (kind, job)
        for kind, job in jobs(kb_dir)
        if job.root_import_id == admission.discovery_intent.root_import_id
        and job.status not in {"completed", "cancelled", "stale"}
    ]
    return {
        "discovery_pending": sum(kind == "discovery" for kind, _ in pending_jobs),
        "imports_pending": sum(kind == "import" for kind, _ in pending_jobs),
    }


@track_import_usage
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
    download_remote_assets: bool | None = None,
    unit_id: str | None = None,
    text_assessment=None,
) -> IngestResult:
    """Caller owns the real KB write lease and frozen input for the whole call."""
    check_stop = context.check_stop if context else lambda: None
    from openkb.import_text import (
        ImportTextRejected,
        preflight_import_text,
        rejection_result,
        validate_text_preflight,
    )

    try:
        if (
            text_assessment is None
            and admission is not None
            and admission.revision.source_format in {"xls", "xlsx"}
            and record_path(kb_dir, "workbooks", admission.revision.source_revision_id).exists()
        ):
            from openkb.workbooks.progress import read_workbook

            text_assessment = preflight_import_text(
                kb_dir,
                prepared,
                check_stop=check_stop,
                workbook=read_workbook(kb_dir, admission.revision),
            )
        text_assessment = text_assessment or preflight_import_text(
            kb_dir, prepared, check_stop=check_stop
        )
        validate_text_preflight(prepared, text_assessment)
    except ImportTextRejected as exc:
        from dataclasses import replace

        return replace(
            rejection_result(prepared, exc),
            source_id=admission.source.source_id if admission else None,
            source_revision_id=admission.revision.source_revision_id if admission else None,
        )
    admission = admission or admit_source_revision(
        kb_dir,
        prepared,
        identity=origin_url,
        check_stop=check_stop,
        text_assessment=text_assessment,
    )
    if prepared.digest != admission.revision.digest:
        raise ValueError("Retained input no longer matches the source revision")
    from openkb.application.version_review import (
        VersionReview,
    )
    from openkb.application.views import bind_source_view
    from openkb.normalization import (
        normalization_fingerprint,
        read_normalization,
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
            **pending_counts(kb_dir, admission),
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
    resource_policy = None
    if admission.revision.source_format in {"html", "htm"}:
        from openkb.remote_assets import resolve_resource_policy

        resource_policy = resolve_resource_policy(kb_dir, download_remote_assets)
    fingerprint = (
        read_normalization(kb_dir, review.normalization_id)[1].fingerprint
        if review
        else admission.revision.reprocessing_policy
        or normalization_fingerprint(
            kb_dir,
            scope=scope,
            bundle=bundle,
            source_revision=admission.revision,
            doc_name=admission.source.doc_name,
            resource_policy=resource_policy,
        )
    )

    if admission.revision.source_format in {"xlsx", "xls"}:
        from openkb.application.workbook_ingestion import import_workbook_units

        return import_workbook_units(
            kb_dir,
            prepared,
            admission=admission,
            fingerprint=fingerprint,
            unit_id=unit_id,
            assessment=assessment,
            review=review,
            scope=scope,
            bundle=bundle,
            context=context,
            on_event=on_event,
            report=report,
            retry_confirmed=retry_confirmed,
        )
    return process_import_unit(
        kb_dir,
        prepared,
        admission=admission,
        fingerprint=fingerprint,
        assessment=assessment,
        review=review,
        scope=scope,
        bundle=bundle,
        context=context,
        on_event=on_event,
        report=report,
        retry_confirmed=retry_confirmed,
        resource_policy=resource_policy,
    )


@track_import_usage
@track_import_unit
def process_import_unit(
    kb_dir,
    prepared,
    *,
    admission,
    fingerprint,
    assessment,
    review,
    scope,
    bundle=None,
    context=None,
    on_event=None,
    report=logger.info,
    retry_confirmed=False,
    resource_policy=None,
    sheet=None,
):
    from openkb.agent.compiler import (
        DEFAULT_COMPILE_CONCURRENCY,
        compile_long_doc,
        compile_short_doc,
    )
    from openkb.application.documents import _run_compile_with_retry
    from openkb.application.version_review import complete_version_review, save_version_wait
    from openkb.indexer import index_long_document
    from openkb.normalization import restore_normalization, retain_normalization

    check_stop = context.check_stop if context else lambda: None
    import hashlib

    from openkb.unit_publication import list_source_units

    for existing in list_source_units(kb_dir, admission.source.source_id):
        if existing.key == (sheet.key if sheet else "body"):
            target = read_record(
                kb_dir, "unit-revisions", existing.target_revision_id, UnitRevision
            )
            if target.source_revision_id == admission.revision.source_revision_id:
                fingerprint = target.processing_fingerprint
            break
    unit, revision = plan_import_units(
        kb_dir,
        admission,
        fingerprint,
        key=sheet.key if sheet else "body",
        name=sheet.name if sheet else None,
        doc_name=admission.source.doc_name
        + "-sheet-"
        + hashlib.sha256(sheet.key.encode()).hexdigest()[:12]
        if sheet
        else None,
    )
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
            kb_dir,
            admission,
            state,
            status="skipped" if state.status in {"completed", "empty", "retired"} else "blocked",
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
            resource_policy=resource_policy,
            unit=unit,
            sheet=sheet,
        )
        if normalized.office_path:
            from openkb.office.slide_content import read_slides, require_slide_navigation

            require_slide_navigation(read_slides(directory / normalized.office_path))
        if assessment.missing_fields:
            state = save_version_wait(kb_dir, admission, normalized, state, assessment)
            return result_from_publication(kb_dir, admission, state, status="blocked")
        if sheet and sheet.content_state == "empty":
            from openkb.workbooks.lifecycle import retire_sheet

            state = retire_sheet(kb_dir, admission, unit, state, "empty", check_stop=check_stop)
            if review:
                try:
                    complete_version_review(kb_dir, review, admission, state)
                except RecoveryRequired:
                    raise
                except Exception:
                    logger.warning(
                        "Worksheet retired but version review completion was not recorded"
                    )
            return result_from_publication(kb_dir, admission, state, status="added")
        from openkb.llm_usage import usage_context

        with (
            usage_context(
                unit_id=unit.unit_id,
                unit_revision_id=revision.unit_revision_id,
                attempt_id=state.job_id,
            ),
            prepare_compile_view(kb_dir, scope.view_id) as view,
        ):
            working = view.scope.wiki_dir.parent
            check_stop()
            with mutation_scope(kb_dir, [working], operation="compile-import-unit"):
                converted = restore_normalization(directory, normalized, working)
                index_ref = None
                if converted.is_long_doc:
                    stage = "indexing"
                    import json

                    from openkb.normalization import normalization_fingerprint
                    from openkb.processing_policy import ReprocessingRequired

                    current_policy = json.loads(
                        normalization_fingerprint(
                            kb_dir,
                            scope=scope,
                            bundle=bundle,
                            source_revision=admission.revision,
                            doc_name=admission.source.doc_name,
                            resource_policy=resource_policy,
                        )
                    )
                    saved_policy = json.loads(fingerprint)
                    if any(
                        saved_policy.get(key) != current_policy[key]
                        for key in ("index_model", "index_policy", "index_sdk", "index_endpoint")
                    ):
                        raise ReprocessingRequired(
                            "Index policy changed; preview reprocess before rebuilding the index"
                        )
                    index_input = converted.pdf_path or converted.raw_path
                    if converted.pdf_path and converted.pdf_path.with_suffix(".okpi").is_file():
                        index_input = converted.pdf_path.with_suffix(".okpi")
                    if (
                        sheet is not None
                        or f".{admission.revision.source_format}" in TEXT_SOURCE_EXTENSIONS
                    ):
                        if converted.source_path is None:
                            raise ValueError("Normalized text is missing")
                        index_input = converted.source_path.with_suffix(".okbi")
                    if index_input is None:
                        raise ValueError("Normalized input is missing")
                    indexed = index_long_document(
                        index_input,
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
            pdf_input = converted.pdf_path or (
                kb_dir / admission.revision.original
                if admission.revision.source_format == "pdf"
                else None
            )
            segmented_pdf = converted.is_long_doc and pdf_input is not None
            extension = "json" if segmented_pdf else "md"
            from openkb.source_map import freeze_pdf_map

            page_map = view.scope.wiki_dir / "sources" / f"{unit.doc_name}.json"
            # A pre-page-map publication can be clarified using its retained
            # conversion. It must not gain invented coverage or be reconverted.
            source_map = (
                freeze_pdf_map(view.scope.wiki_dir, unit.doc_name, pdf_input)
                if pdf_input is not None and (converted.processing is not None or page_map.exists())
                else None
            )
            if (
                sheet is not None
                or f".{admission.revision.source_format}" in TEXT_SOURCE_EXTENSIONS
            ):
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
                normalized_format="pdf" if segmented_pdf else "markdown",
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
        return result_from_publication(
            kb_dir, admission, failed, status="blocked" if failed.status == "blocked" else "failed"
        )
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
