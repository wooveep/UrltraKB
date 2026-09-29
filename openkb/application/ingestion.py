"""Frozen-source import: admission survives each independently published unit."""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Callable, Literal

from openkb.application.execution import ExecutionContext
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.converter import convert_document
from openkb.ingest_records import UnitPublication, UnitRevision
from openkb.ingest_result import ImportUnitOutcome, IngestResult
from openkb.inputs import PreparedInput
from openkb.locks import LockCancelled
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.source_catalog import Admission, admit_source_revision, read_record
from openkb.unit_publication import (
    begin_unit_attempt,
    plan_import_units,
    prepare_compile_view,
    publish_unit_revision,
    record_unit_failure,
)

logger = logging.getLogger(__name__)


def result_from_publication(
    kb_dir: Path,
    admission: Admission,
    state: UnitPublication,
    *,
    status: Literal["added", "skipped", "failed", "blocked", "partial", "stopped"],
) -> IngestResult:
    resources = [str(kb_dir / admission.revision.original)]
    actual_source = None
    if state.successful_revision_id:
        actual = read_record(kb_dir, "unit-revisions", state.successful_revision_id, UnitRevision)
        actual_source = actual.source_revision_id
    if state.knowledge_revision_id:
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
    )
    return IngestResult(
        admission.source.identity,
        status,
        tuple(resources),
        unfinished=() if status in {"added", "skipped"} else (state.stage,),
        input_version=admission.revision.digest,
        source_id=admission.source.source_id,
        source_revision_id=admission.revision.source_revision_id,
        units=(outcome,),
        discovery_pending=int(admission.discovery_intent.status == "pending"),
        message=state.message,
    )


def import_prepared_pdf(
    kb_dir: Path,
    prepared: PreparedInput,
    *,
    bundle=None,
    context: ExecutionContext | None = None,
    on_event: Callable[[dict], None] | None = None,
    origin_url: str | None = None,
    report=logger.info,
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
    admission = admit_source_revision(kb_dir, prepared, identity=origin_url, check_stop=check_stop)
    unit, revision = plan_import_units(kb_dir, admission, "pdf-content-pipeline-v1")
    state, runnable = begin_unit_attempt(
        kb_dir, unit, revision, discovery_intent=admission.discovery_intent
    )
    if not runnable:
        return result_from_publication(
            kb_dir, admission, state, status="skipped" if state.status == "completed" else "blocked"
        )
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    concurrency = resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY
    stage = "conversion"
    try:
        with prepare_compile_view(kb_dir) as view:
            working = view.scope.wiki_dir.parent
            check_stop()
            with mutation_scope(kb_dir, [working], operation="compile-import-unit"):
                if on_event:
                    on_event({"stage": "converting", "source": admission.source.name})
                converted = convert_document(
                    prepared.source,
                    kb_dir,
                    staging_dir=working,
                    prepared=prepared,
                    doc_name=unit.doc_name,
                )
                index_ref = None
                if converted.is_long_doc:
                    stage = "indexing"
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
            state = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.{extension}",
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
    return result_from_publication(
        kb_dir, admission, state, status="added" if state.status == "completed" else "blocked"
    )
