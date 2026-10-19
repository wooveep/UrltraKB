"""Complete-inventory worksheet exclusions share the ordinary publication protocol."""

from typing import Literal

from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
from ultrakb.source_records import Record
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.source_changes import SourceChange, committed_source_change
from ultrakb.storage.workbook_store import WorkbookStore

from .publications import sync_publication
from .task_stages import task_stage


class MissingSheet(Record):
    key: str
    name: str
    ordinal: int
    content_state: Literal["withdrawn"] = "withdrawn"
    error: str | None = None


def missing_sheets(scope, catalog, revision, workbook):
    keys = {sheet.key for sheet in workbook.sheets}
    for target in catalog.source_units(revision.source_id):
        if target.key == "body" or target.key in keys:
            continue
        processing = catalog.processing(target.processing_id)
        previous = WorkbookStore(scope.kb_dir, scope.knowledge.identity).read(
            target.source_revision_id, processing_key=processing.reprocessing_request_id or ""
        )
        sheet = (
            next((sheet for sheet in previous.sheets if sheet.key == target.key), None)
            if previous
            else None
        )
        if sheet is None and processing.content_state == "withdrawn":
            sheet = MissingSheet.model_validate_json(
                catalog.read_processed(target.processing_id, processing.source_path)
            )
        yield MissingSheet(
            key=target.key,
            name=sheet.name if sheet else target.doc_name,
            ordinal=sheet.ordinal if sheet else 1,
        )


def retire_worksheet(scope, ready, context, catalog, revision, target, sheet):
    base = scope.knowledge
    store = KnowledgeStore(scope.kb_dir, base.identity)
    retained = committed_source_change(
        store,
        base,
        revision.source_id,
        kind=sheet.content_state,
        unit_revision_id=target.unit_revision_id,
    )
    reused = retained is not None
    prepared = retained[0] if retained else None
    publication = None
    active = set(base.unit_revisions())
    actual = next(
        (
            unit
            for unit in sorted(
                base.input_units(), key=lambda unit: unit.unit_revision_id not in active
            )
            if unit.unit_id == target.unit_id
        ),
        None,
    )
    unconfirmed = ()
    try:
        with task_stage(
            context,
            "worksheet.retire.v1",
            root=scope.kb_dir,
            source_revision_id=revision.revision_id,
            unit_revision_id=target.unit_revision_id,
        ):
            if prepared is None:
                source = catalog.source(revision.source_id)
                claim = SourceChange(
                    source_id=revision.source_id,
                    source_revision_id=revision.revision_id,
                    source_generation=source.generation,
                    kind=sheet.content_state,
                    unit_revision_id=target.unit_revision_id,
                )
                prepared = store.prepare_changes(base, {}, source_change=claim)
            try:
                publication = store.publish(
                    prepared,
                    check_cancelled=context.check_stop,
                    execution_id=context.storage_execution.execution_id,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
    except Exception:
        if publication is None:
            raise
        unconfirmed = ("confirm worksheet retirement observation",)
    projection = sync_publication(base.root, base.identity, prepared)
    pending = projection.status not in {"synced", "obsolete"}
    unfinished = (*(("synchronize wiki",) if pending else ()), *unconfirmed)
    result = IngestResult(
        str(ready.identity),
        "skipped" if reused else "added",
        (),
        source_id=revision.source_id,
        source_revision_id=revision.revision_id,
        input_version=ready.digest,
        units=(
            ImportUnitOutcome(
                target.unit_id,
                "empty" if sheet.content_state == "empty" else "retired",
                target.unit_revision_id,
                successful_revision_id=actual.unit_revision_id if actual else None,
                successful_source_revision_id=actual.source_revision_id if actual else None,
                knowledge_revision_id=publication.revision_id if actual else None,
                view_id=scope.view_id,
                key=target.key,
                name=sheet.name,
                doc_name=target.doc_name,
                unfinished=unfinished,
                message="Worksheet contribution withdrawn; historical evidence retained.",
            ),
        ),
        unfinished=unfinished,
        publication_id=publication.publication_id,
        projection_pending=pending,
    )
    return result, scope.publication_id if reused else publication.publication_id
