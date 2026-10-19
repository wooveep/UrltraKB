"""Run the existing compiler against retained inputs and an immutable publication."""

import asyncio
import logging
import time
from contextlib import ExitStack
from dataclasses import replace

from ultrakb.compilation_report import compilation_attempt
from ultrakb.context.workers import run_blocking
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.llm_usage import usage_context
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_inputs import materialize_inputs, prepare_compiled_revision
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.pending_store import PendingStore
from ultrakb.storage.wiki_projection import ProjectionConflict

from .publications import sync_publication
from .recompilation import RecompileResult
from .task_stages import task_stage

logger = logging.getLogger(__name__)


async def recompile_stored(scope, source_id, *, unit_id, context, version, model, max_concurrency):
    base = scope.knowledge
    if version is not None and version != base.publication_id:
        return RecompileResult("conflict", message="Knowledge changed after confirmation")
    if unit_id is None:
        return RecompileResult("blocked", message="Select an explicit retained processing unit")
    target = CatalogStore(scope.kb_dir, base.identity).unit(unit_id)
    if target.source_id != source_id:
        raise ValueError("The compilation unit belongs to another source")
    processing = CatalogStore(scope.kb_dir, base.identity).processing(target.processing_id)
    if processing.content_state != "content":
        return RecompileResult(
            "blocked",
            target.doc_name,
            message="Original retained; no reliable content to compile "
            f"({processing.content_state})",
        )
    active = set(base.unit_revisions())
    previous = next(
        (
            unit
            for unit in sorted(
                base.input_units(), key=lambda unit: unit.unit_revision_id not in active
            )
            if unit.unit_id == target.unit_id
        ),
        None,
    )
    previous = (
        (
            previous,
            CatalogStore(scope.kb_dir, base.identity).processing(previous.processing_id).decision,
        )
        if previous
        else None
    )
    if context.storage_identity not in (None, base.identity):
        raise ValueError("The compilation context belongs to another database generation")
    context.storage_identity = base.identity
    started, result, execution_began = time.monotonic(), None, False

    def worker(bundle):
        async def compile_owned():
            nonlocal result
            result = await _compile(scope, target, context, bundle, model, max_concurrency)

        asyncio.run(compile_owned())

    try:
        with context.begin(scope.kb_dir) as bundle:
            execution_began = True
            with usage_context(
                source_id=target.source_id,
                source_revision_id=target.source_revision_id,
                unit_id=target.unit_id,
                unit_revision_id=target.unit_revision_id,
                root_import_id=context.storage_execution.model_budget_id,
            ):
                await run_blocking(lambda: worker(bundle))
    except (asyncio.CancelledError, Exception) as error:
        if result is not None and result.status == "compiled":
            result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        else:
            result = RecompileResult(
                "stopped"
                if isinstance(error, (LockCancelled, asyncio.CancelledError))
                else "blocked",
                target.doc_name,
                message=str(error),
                error_type=type(error).__name__,
                unfinished=("compilation",),
            )
    return _result_receipt(
        scope,
        target,
        processing.decision,
        previous,
        result,
        context if execution_began else None,
        started,
    )


def _result_receipt(scope, target, decision, previous, result, context, started):
    from ultrakb.ingest_result import ImportUnitOutcome

    from .result_usage import confirm_result_usage

    compiled = result.status == "compiled"
    actual, actual_decision = (target, decision) if compiled else previous or (None, None)
    unit = ImportUnitOutcome(
        target.unit_id,
        "completed" if compiled else result.status,
        target.unit_revision_id,
        successful_revision_id=actual.unit_revision_id if actual else None,
        successful_source_revision_id=actual.source_revision_id if actual else None,
        knowledge_revision_id=result.knowledge_revision_id
        if compiled
        else (scope.knowledge.publication.revision_id if actual else None),
        view_id=scope.view_id,
        key=target.key,
        name=target.doc_name,
        doc_name=target.doc_name,
        length_class=decision.length_class,
        execution_mode=decision.execution_mode,
        processing=actual_decision.model_dump(mode="json") if actual_decision else None,
        target_processing=decision.model_dump(mode="json"),
        error_type=result.error_type,
        message=result.message,
        unfinished=result.unfinished,
    )
    unit = confirm_result_usage(
        unit,
        context,
        scope.kb_dir,
        scope.knowledge.identity,
        source_id=target.source_id,
        unit_id=target.unit_id,
    )
    result = replace(
        result,
        units=(unit,),
        kind=decision.length_class,
        execution_mode=decision.execution_mode,
        elapsed=time.monotonic() - started,
        unfinished=tuple(dict.fromkeys((*result.unfinished, *unit.unfinished))),
    )
    return confirm_result_usage(
        result,
        context,
        scope.kb_dir,
        scope.knowledge.identity,
        source_id=target.source_id,
    )


async def _compile(scope, target, context, bundle, model, max_concurrency):
    from ultrakb.agent.compiler import compile_long_doc, compile_short_doc
    from ultrakb.storage.index_store import IndexStore

    base = scope.knowledge
    execution = context.storage_execution
    config = context.snapshot.values()["effective"]
    store = KnowledgeStore(base.root, base.identity)
    pending = PendingStore(base.root, base.identity)
    work = pending.enqueue(
        target.source_revision_id,
        unit_revision_id=target.unit_revision_id,
        root_import_id=execution.model_budget_id,
    )
    if work.status in {"failed", "blocked", "interrupted", "stopped"}:
        work = pending.retry(work.intent_id)
    if work.status == "pending":
        work = pending.start(pending.claim(intent_id=work.intent_id))
    if work is None or work.status not in {"started", "completed"}:
        raise RecoveryRequired("The compilation claim is no longer available")
    prepared = store.committed_import(work, view_id=scope.view_id)
    accepted = None
    try:
        if prepared is None:
            current = store.bind(view_id=scope.view_id)
            if current.publication_id != base.publication_id:
                raise PublicationConflict(
                    "Knowledge changed before compilation; review the current publication"
                )
            processing = CatalogStore(base.root, base.identity).processing(target.processing_id)
            indexes = IndexStore(base.root, base.identity)
            index = next(
                (
                    item
                    for item in base.index_revisions()
                    if indexes.revision(item).unit_revision_id == target.unit_revision_id
                ),
                None,
            )
            segmented = processing.decision.execution_mode == "segmented"
            checkpoints = CompileCheckpointStore(base.root, base.identity).for_work(
                execution.model_budget_id,
                object_id={
                    "unit_revision_id": target.unit_revision_id,
                    "view_id": scope.view_id,
                    "publication_id": base.publication_id,
                },
                conditions={"fingerprint": context.executor.fingerprint, "compiler": 3},
                execution_id=execution.execution_id,
            )
            with ExitStack() as temporary:
                with task_stage(
                    context,
                    "compile.inputs.v1",
                    source_revision_id=target.source_revision_id,
                    unit_revision_id=target.unit_revision_id,
                ):
                    baseline = temporary.enter_context(materialize_baseline(base, ()))
                    inputs = materialize_inputs(base, baseline, target)
                if segmented and index is None:
                    from .storage_indexing import prepare_document_index

                    index = prepare_document_index(
                        base, target, processing, baseline, context, bundle, model
                    )
                private = KnowledgeScope(
                    kb_dir=base.root,
                    view_id=scope.view_id,
                    wiki_dir=baseline.directory,
                    private_workspace=baseline.private,
                )
                with (
                    task_stage(
                        context,
                        "compile.generate.v1",
                        source_revision_id=target.source_revision_id,
                        unit_revision_id=target.unit_revision_id,
                    ),
                    compile_binding(
                        checkpoints, view_id=scope.view_id, document=target.doc_name, inputs=inputs
                    ),
                    compilation_attempt() as report,
                ):
                    options = {
                        "max_concurrency": max_concurrency or config.get("compile_concurrency", 4),
                        "bundle": bundle,
                        "scope": private,
                    }
                    if segmented:
                        document = indexes.document(index)
                        await compile_long_doc(
                            target.doc_name,
                            baseline.directory / f"summaries/{target.doc_name}.md",
                            document["doc_id"],
                            base.root,
                            model or config["model"],
                            navigation=document["structure"],
                            **options,
                        )
                    else:
                        await compile_short_doc(
                            target.doc_name,
                            baseline.directory / f"sources/{target.doc_name}.md",
                            base.root,
                            model or config["model"],
                            **options,
                        )
                with task_stage(
                    context, "compile.validate.v1", unit_revision_id=target.unit_revision_id
                ):
                    report.require_complete()
                    context.check_stop()
                with task_stage(
                    context, "compile.persist.v1", unit_revision_id=target.unit_revision_id
                ):
                    prepared = prepare_compiled_revision(
                        store,
                        base,
                        baseline,
                        target,
                        work,
                        index_revisions=(index,) if index else (),
                    )
        with task_stage(context, "knowledge.publish.v1", unit_revision_id=target.unit_revision_id):
            try:
                publication = store.publish(
                    prepared,
                    check_cancelled=context.check_stop,
                    execution_id=execution.execution_id,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
            accepted = RecompileResult(
                "compiled",
                target.doc_name,
                version=publication.publication_id,
                knowledge_revision_id=publication.revision_id,
                unfinished=("synchronize wiki",),
            )
        with task_stage(context, "wiki.sync.v1", unit_revision_id=target.unit_revision_id):
            projection = sync_publication(base.root, base.identity, prepared)
        return replace(
            accepted, unfinished=() if projection.status == "synced" else ("synchronize wiki",)
        )
    except BaseException as error:
        if accepted is not None:
            context._storage_result = "completed"
            return replace(accepted, message="Published; file synchronization needs confirmation")
        context._storage_result = (
            "cancelled" if isinstance(error, (LockCancelled, asyncio.CancelledError)) else "failed"
        )
        try:
            pending.finish(
                work,
                status="stopped" if context._storage_result == "cancelled" else "failed",
                error_code=type(error).__name__,
            )
        except Exception:
            logger.warning("Could not confirm compilation failure", exc_info=True)
        if isinstance(error, (CatalogConflict, PublicationConflict, ProjectionConflict)):
            return RecompileResult("conflict", target.doc_name, message=str(error))
        raise
