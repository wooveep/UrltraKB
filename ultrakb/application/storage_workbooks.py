"""Plan every retained worksheet before independently compiling content units."""

import asyncio
import hashlib

from ultrakb.compile_checkpoints import digest
from ultrakb.execution_capacity import select_execution_mode
from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.locks import LockCancelled, atomic_write_json, atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.stage_observations import InputObservation
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.workbook_store import WorkbookStore
from ultrakb.workbooks.normalization import freeze_sheet

from .storage_ingestion import compile_stored_unit
from .storage_processing_policy import (
    POLICY_PATH,
    ProcessingPolicy,
    current_processing_policy,
    needs_reprocessing,
)
from .storage_worksheet_retirement import missing_sheets, retire_worksheet
from .task_stages import task_stage
from .views import storage_scope
from .workbook_ingestion import aggregate_units


def _plan(
    scope,
    ready,
    context,
    bundle,
    catalog,
    revision,
    sheet,
    policy,
    reprocessing,
    source_generation,
):
    target = catalog.unit_for_source(revision.source_id, key=sheet.key)
    forced = reprocessing is not None and needs_reprocessing(catalog, target, reprocessing)
    if target is not None and target.source_revision_id == revision.revision_id and not forced:
        return target, False
    from ultrakb.converter import _sanitize_stem

    doc_name = (
        target.doc_name
        if target
        else (
            f"{_sanitize_stem(ready.source.stem)}-{revision.source_id[:8]}-sheet-"
            + hashlib.sha256(sheet.key.encode()).hexdigest()[:12]
        )
    )
    original = next(file for file in revision.files if file.kind == "main")
    measured = sheet.content_state in {"content", "empty"}
    with task_stage(
        context,
        "worksheet.normalize.v1",
        source_revision_id=revision.revision_id,
        work_key=sheet.key,
        ordinal=sheet.ordinal,
        observation_input=InputObservation(
            input_name=original.path,
            input_bytes=original.byte_count,
            input_digest=original.digest,
            unit_label=sheet.name,
            item_count=len(sheet.cells) if measured else None,
            item_kind="cells" if measured else None,
        ),
    ):
        with materialize_baseline(scope.knowledge, ()) as baseline:
            body = baseline.directory / f"sources/{doc_name}.md"
            native = body.with_suffix(".content.json")
            content = sheet.content_state in {"content", "empty"}
            frozen = freeze_sheet(ready, sheet) if content else None
            atomic_write_text(body, frozen.text if frozen else "")
            decision = classify_markdown_tokens(frozen.tokens if frozen and sheet.cells else 0)
            if sheet.content_state == "content":
                private = KnowledgeScope(
                    scope.kb_dir,
                    baseline.directory,
                    scope.view_id,
                    private_workspace=baseline.private,
                )
                decision = select_execution_mode(
                    scope.kb_dir, decision, body, doc_name, scope=private, bundle=bundle
                )
                if decision.execution_mode == "segmented":
                    from ultrakb.block_package import freeze_content_blocks

                    frozen = freeze_content_blocks(frozen.model_dump(mode="json"))
            raw = frozen.model_dump(mode="json") if frozen else sheet.model_dump(mode="json")
            atomic_write_json(native, raw)
            recipe = ProcessingPolicy(
                values=reprocessing.policy
                if reprocessing
                else current_processing_policy(scope, catalog, revision, doc_name, bundle=bundle),
                request_id=reprocessing.request_id if reprocessing else None,
            )
            policy_path = baseline.directory / POLICY_PATH
            atomic_write_json(policy_path, recipe.model_dump(mode="json"))
            fingerprint = digest(
                {
                    "native": raw,
                    "policy": policy,
                    "decision": decision.model_dump(mode="json"),
                    "recipe": recipe.model_dump(mode="json"),
                }
            )
            processing = catalog.retain_processing(
                revision.revision_id,
                {body.name: body, native.name: native, POLICY_PATH: policy_path},
                body_path=body.name,
                source_path=native.name,
                source_kind="text",
                fingerprint=fingerprint,
                operation_id=digest([revision.revision_id, sheet.key, fingerprint]),
                decision=decision,
                content_state=sheet.content_state,
                reprocessing_request_id=reprocessing.request_id if reprocessing else None,
            )
            context.check_stop()
            planned = catalog.plan_unit(
                processing.processing_id,
                key=sheet.key,
                doc_name=doc_name,
                label=sheet.name,
                expected_generation=target.generation if target else 0,
                expected_source_generation=None if reprocessing else source_generation,
            )
            return planned, target is None or target.unit_revision_id != planned.unit_revision_id


def _uncompiled(scope, ready, revision, target, sheet, *, error=None, status=None):
    status = status or ("empty" if sheet.content_state == "empty" else "failed")
    return IngestResult(
        str(ready.identity),
        "stopped"
        if status == "stopped"
        else "blocked"
        if status in {"empty", "not_run"}
        else "failed",
        (),
        source_id=revision.source_id,
        source_revision_id=revision.revision_id,
        input_version=ready.digest,
        units=(
            ImportUnitOutcome(
                target.unit_id,
                status,
                target.unit_revision_id,
                view_id=scope.view_id,
                key=sheet.key,
                name=sheet.name,
                doc_name=target.doc_name,
                message=str(error)
                if error
                else sheet.error
                or f"Worksheet content: {sheet.content_state}; no knowledge generated",
                unfinished=() if status == "empty" else ("worksheet processing",),
            ),
        ),
        unfinished=() if status == "empty" else ("worksheet processing",),
        message=str(error) if error else sheet.error,
    )


def import_workbook(
    scope,
    ready,
    context,
    bundle,
    catalog,
    revision,
    *,
    source_generation,
    unit_id=None,
    reprocessing=None,
):
    with task_stage(
        context, "workbook.inventory.v1", source_revision_id=revision.revision_id
    ) as progress:
        workbook = WorkbookStore(scope.kb_dir, scope.knowledge.identity).retain(
            revision, ready, processing_key=reprocessing.request_id if reprocessing else ""
        )
        if workbook.error:
            progress.update(status="failed", reason_code="WorkbookInventoryFailed")
    if workbook.error:
        return IngestResult(
            str(ready.identity),
            "failed",
            (),
            source_id=revision.source_id,
            source_revision_id=revision.revision_id,
            message=workbook.error,
            unfinished=("worksheet inventory",),
        )
    # Retention and target planning finish before the first model call, so a
    # failed/cancelled first worksheet cannot hide the remaining inventory.
    targets = []
    for sheet in (*workbook.sheets, *missing_sheets(scope, catalog, revision, workbook)):
        target, changed = _plan(
            scope,
            ready,
            context,
            bundle,
            catalog,
            revision,
            sheet,
            workbook.policy,
            reprocessing,
            source_generation,
        )
        targets.append((target, sheet))
        source_generation += int(changed)
    if unit_id is not None and not any(target.unit_id == unit_id for target, _ in targets):
        raise ValueError("Worksheet does not belong to this source revision")
    if unit_id is not None:
        targets = [(target, sheet) for target, sheet in targets if target.unit_id == unit_id]
    results, current = [], scope
    publication_id = scope.publication_id
    for index, (target, sheet) in enumerate(targets):
        try:
            context.check_stop()
            if current.publication_id != publication_id:
                current = storage_scope(
                    scope.kb_dir,
                    scope.knowledge.identity,
                    publication_id=publication_id,
                    view_id=scope.view_id,
                )
            if sheet.content_state in {"empty", "withdrawn"}:
                result, publication_id = retire_worksheet(
                    current, ready, context, catalog, revision, target, sheet
                )
            elif sheet.content_state != "content":
                with task_stage(
                    context,
                    "worksheet.validate.v1",
                    source_revision_id=revision.revision_id,
                    unit_revision_id=target.unit_revision_id,
                    work_key=sheet.key,
                    ordinal=sheet.ordinal,
                ) as progress:
                    result = _uncompiled(current, ready, revision, target, sheet)
                    progress.update(status="failed", reason_code=sheet.content_state)
            else:
                result, accepted = compile_stored_unit(
                    current, ready, context, catalog, revision, target, name=sheet.name
                )
                if result.status == "added":
                    publication_id = accepted
        except (Exception, asyncio.CancelledError) as error:
            status = (
                "stopped"
                if isinstance(error, (LockCancelled, asyncio.CancelledError))
                else "failed"
            )
            result = _uncompiled(
                current, ready, revision, target, sheet, error=error, status=status
            )
        results.append(result)
        from .connection_failures import shared_model_failure

        failure = shared_model_failure(getattr(context.executor, "model_errors", ()))
        if result.status == "stopped" or failure is not None:
            for pending, remaining in targets[index + 1 :]:
                results.append(
                    _uncompiled(current, ready, revision, pending, remaining, status="not_run")
                )
            break
    return aggregate_units(results)
