"""Run the existing compiler against retained inputs and an immutable publication."""

import asyncio
import logging
import time
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
    if context.storage_identity not in (None, base.identity):
        raise ValueError("The compilation context belongs to another database generation")
    context.storage_identity = base.identity
    started, result = time.monotonic(), None

    def worker(bundle):
        async def compile_owned():
            nonlocal result
            result = await _compile(scope, target, context, bundle, model, max_concurrency)

        asyncio.run(compile_owned())

    try:
        with context.begin(scope.kb_dir) as bundle:
            with usage_context(
                source_id=target.source_id,
                source_revision_id=target.source_revision_id,
                unit_id=target.unit_id,
                unit_revision_id=target.unit_revision_id,
                root_import_id=context.storage_execution.model_budget_id,
            ):
                await run_blocking(lambda: worker(bundle))
        return replace(result, elapsed=time.monotonic() - started)
    except (asyncio.CancelledError, Exception) as error:
        if result is not None and result.status == "compiled":
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        return RecompileResult(
            "stopped" if isinstance(error, (LockCancelled, asyncio.CancelledError)) else "blocked",
            target.doc_name,
            message=str(error),
            error_type=type(error).__name__,
            elapsed=time.monotonic() - started,
            unfinished=("compilation",),
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
            if segmented and index is None:
                raise RecoveryRequired(
                    "Long document compilation requires its accepted shared index"
                )
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
            with materialize_baseline(base, ()) as baseline:
                inputs = materialize_inputs(base, baseline, target)
                private = KnowledgeScope(
                    kb_dir=base.root,
                    view_id=scope.view_id,
                    wiki_dir=baseline.directory,
                    private_workspace=baseline.private,
                )
                with (
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
                report.require_complete()
                context.check_stop()
                prepared = prepare_compiled_revision(store, base, baseline, target, work)
        try:
            publication = store.publish(
                prepared, check_cancelled=context.check_stop, execution_id=execution.execution_id
            )
        except Exception:
            publication = store.committed_publication(prepared)
            if publication is None:
                raise
        accepted = RecompileResult(
            "compiled",
            target.doc_name,
            version=publication.publication_id,
            unfinished=("synchronize wiki",),
        )
        projection = sync_publication(base.root, base.identity, prepared)
        return replace(
            accepted, unfinished=() if projection.status == "synced" else ("synchronize wiki",)
        )
    except BaseException as error:
        context._storage_result = "failed"
        if accepted is not None:
            return replace(accepted, message="Published; file synchronization needs confirmation")
        try:
            pending.finish(work, status="failed", error_code=type(error).__name__)
        except Exception:
            logger.warning("Could not confirm compilation failure", exc_info=True)
        if isinstance(error, (CatalogConflict, PublicationConflict, ProjectionConflict)):
            return RecompileResult("conflict", target.doc_name, message=str(error))
        raise
