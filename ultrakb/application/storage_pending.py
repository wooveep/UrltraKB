"""Shared main-store dispatcher with exact claims and independently retained results."""

import asyncio
from dataclasses import asdict, replace

from ultrakb.llm_usage import usage_context
from ultrakb.locks import LockCancelled
from ultrakb.pending.budget import BudgetWait
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.pending_results import PendingResults
from ultrakb.storage.pending_store import PendingStore

from .execution import ExecutionContext
from .storage_execution import begin_storage_operation
from .storage_pending_queries import stored_job, stored_pending_status
from .task_stages import task_stage
from .views import storage_scope


def claim_stored_job(scope, *, cancelled=None):
    work = PendingStore(scope.kb_dir, scope.knowledge.identity).claim(cancelled=cancelled)
    return (
        {"id": work.intent_id, "kind": work.kind, "dispatch_id": work.dispatch_id} if work else None
    )


def _context(scope, work, context):
    if context.storage_identity not in (None, scope.knowledge.identity):
        raise ValueError("Pending execution belongs to another database generation")
    store = ExecutionStore(scope.kb_dir, scope.knowledge.identity)
    resume = store.pending_execution(work.intent_id) if work.kind == "import" else None
    if resume is None and work.kind == "import" and work.unit_revision_id is not None:
        resume = store.source_execution(work.source_revision_id)
    return replace(
        context,
        storage_identity=scope.knowledge.identity,
        executor=None,
        resume_storage_execution_id=resume,
        storage_pending_attempt_id=work.attempt_id,
    )


def _unit_import(scope, work, context):
    from .storage_ingestion import compile_stored_unit

    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    target = catalog.unit_revision(work.unit_revision_id)
    revision = catalog.revision(work.source_revision_id)
    from .storage_source_retry import retained_input

    with (
        retained_input(catalog, revision) as ready,
        usage_context(root_import_id=work.root_import_id),
    ):
        return compile_stored_unit(scope, ready, context, catalog, revision, target)[0]


def run_stored_job(scope, intent_id, dispatch_id, context=None):
    parent = context or ExecutionContext()
    if parent.storage_identity not in (None, scope.knowledge.identity):
        raise ValueError("Pending execution belongs to another database generation")
    pending = PendingStore(scope.kb_dir, scope.knowledge.identity)
    before = pending.job(intent_id)
    if before.dispatch_id != dispatch_id or before.status != "dispatched":
        return {**stored_job(scope, intent_id, details=True), "dispatch_status": "skipped"}
    work = pending.start(before, new_only=True)
    if work is None:
        return {**stored_job(scope, intent_id), "dispatch_status": "skipped"}
    context = parent
    result, delivered = None, False
    try:
        context = _context(scope, work, parent)
        if work.kind == "discovery":
            from .storage_discovery import discover_stored

            with begin_storage_operation(scope.kb_dir, context, "discover-embedded"):
                with task_stage(
                    context,
                    "discovery.run.v1",
                    root=scope.kb_dir,
                    source_revision_id=work.source_revision_id,
                ):
                    discover_stored(scope, work, context)
                    context._storage_result = "completed"
        else:
            with context.begin(scope.kb_dir) as bundle:
                if work.unit_revision_id is not None:
                    result = _unit_import(scope, work, context)
                    delivered = result.status in {"added", "skipped"}
                    if not delivered:
                        pending.finish(
                            work, status="stopped" if result.status == "stopped" else "blocked"
                        )
                else:
                    from .storage_pending_imports import import_recovered

                    result = import_recovered(scope, work, context, bundle)
                    PendingResults(scope.kb_dir, scope.knowledge.identity).save(
                        work, context.storage_execution, result
                    )
                    delivered = True
                context._storage_result = (
                    "completed"
                    if result.status in {"added", "skipped", "rejected"}
                    else "cancelled"
                    if result.status == "stopped"
                    else "failed"
                )
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            result = replace(
                result,
                unfinished=(
                    *result.unfinished,
                    "confirm pending execution cleanup" if delivered else "confirm pending result",
                ),
            )
        else:
            try:
                pending.finish(
                    work,
                    status="budget_wait"
                    if isinstance(error, BudgetWait)
                    else "stopped"
                    if isinstance(error, (LockCancelled, asyncio.CancelledError))
                    else "failed",
                    error_code=type(error).__name__,
                    message=str(error)[:1000],
                )
            except Exception:
                pass  # The original started claim remains visible for explicit recovery.
            value = stored_job(scope, intent_id, details=True)
            value["message"] = str(error)
            return value
    finally:
        if parent.snapshot is None:
            parent.snapshot = context.snapshot
    try:
        value = stored_job(scope, intent_id, details=True)
    except Exception:
        if result is None:
            raise
        value = {
            "id": intent_id,
            "kind": work.kind,
            "status": "completed" if delivered else "interrupted",
        }
    if result is not None:
        value.update(
            result=asdict(result),
            model_usage=result.model_usage,
            publication_id=result.publication_id,
            projection_pending=result.projection_pending,
            unfinished=list(result.unfinished),
        )
    return value


def process_stored_pending(scope, *, max_jobs, context=None):
    context = context or ExecutionContext()
    outcomes = []
    for _ in range(max_jobs):
        context.check_stop()
        selected = claim_stored_job(scope)
        if selected is None:
            break
        current = storage_scope(scope.kb_dir, scope.knowledge.identity, view_id=scope.view_id)
        outcomes.append(run_stored_job(current, selected["id"], selected["dispatch_id"], context))
    return {"processed": len(outcomes), "outcomes": outcomes, **stored_pending_status(scope)}
