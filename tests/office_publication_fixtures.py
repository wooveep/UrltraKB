"""Explicit worksheet fixture publications from the actual native import pipeline."""

from dataclasses import replace

from native_publication_fixtures import publication_workspace
from processing_fixtures import source_processing_context

from ultrakb.agent.compiler import _write_summary
from ultrakb.application.documents import import_document
from ultrakb.application.views import published_scope
from ultrakb.config import LlmCredentialBundle
from ultrakb.selected_refresh import _refresh_index
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_inputs import source_paths


def publish_workbook(
    kb,
    path,
    *,
    bundle=None,
    admission=None,
    retry_confirmed=False,
    sheet_names=None,
    record_model_task=True,
):
    context = source_processing_context(
        bundle=bundle or LlmCredentialBundle(api_key="unused-fixture")
    )
    retained = import_document(kb, path, context=context)
    assert retained.source_id is not None and context.executor.calls == 0, retained
    catalog = CatalogStore(kb, published_scope(kb).knowledge.identity)
    completed, outcomes = {}, {}
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.storage_execution import begin_storage_operation
    from ultrakb.application.storage_source_retry import retained_input
    from ultrakb.application.storage_worksheet_retirement import MissingSheet, retire_worksheet
    from ultrakb.storage.workbook_store import WorkbookStore

    revision = catalog.revision(retained.source_revision_id)
    workbook = WorkbookStore(kb, catalog.identity).read(revision.revision_id)
    sheets = {sheet.key: sheet for sheet in workbook.sheets}
    for target in catalog.source_units(retained.source_id):
        if sheet_names is not None and target.label not in sheet_names:
            continue
        processing = catalog.processing(target.processing_id)
        if processing.content_state in {"empty", "withdrawn"}:
            sheet = sheets.get(target.key)
            if sheet is None:
                sheet = MissingSheet.model_validate_json(
                    catalog.read_processed(processing.processing_id, processing.source_path)
                )
            control = ExecutionContext(storage_identity=catalog.identity)
            with begin_storage_operation(kb, control, "fixture-worksheet-retirement"):
                with retained_input(catalog, revision) as ready:
                    result, _ = retire_worksheet(
                        published_scope(kb), ready, control, catalog, revision, target, sheet
                    )
                outcomes[target.unit_id] = result.units[0]
                control._storage_result = "completed"
            continue
        if processing.content_state != "content":
            continue
        with publication_workspace(kb, target.unit_revision_id) as (wiki, target):
            body = catalog.read_processed(processing.processing_id, processing.body_path).decode()
            _write_summary(
                wiki,
                target.doc_name,
                body,
                description=target.label or target.doc_name,
                full_text=source_paths(target, processing)[-1][0],
            )
            _refresh_index(wiki)
        completed[target.unit_id] = published_scope(kb).knowledge.publication.revision_id
    units = tuple(
        outcomes[item.unit_id]
        if item.unit_id in outcomes
        else replace(
            item,
            status="completed",
            unfinished=(),
            error_type=None,
            knowledge_revision_id=completed[item.unit_id],
            successful_revision_id=item.target_revision_id,
            successful_source_revision_id=retained.source_revision_id,
        )
        if item.unit_id in completed
        else item
        for item in retained.units
    )
    return replace(
        retained,
        status="added"
        if all(unit.status in {"completed", "empty", "retired"} for unit in units)
        else "partial",
        units=units,
        unfinished=(),
        message=None,
    )
