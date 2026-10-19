"""Confirm a source's empty contribution without discarding original or page evidence."""

from dataclasses import replace

from ultrakb.ingest_diagnostics import failure_reason
from ultrakb.locks import LockCancelled
from ultrakb.refresh_publication import RefreshResult
from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.source_changes import SourceChange, committed_source_change

from .execution import ExecutionContext
from .publications import publish_knowledge_changes, sync_publication
from .storage_execution import begin_storage_operation
from .task_stages import task_stage


def _completed(scope, prepared, projection):
    pending = projection.status not in {"synced", "obsolete"}
    return RefreshResult(
        "completed",
        scope.view_id,
        prepared.revision_id,
        message="Empty contribution confirmed; dependent knowledge needs refresh.",
        resources=("wiki",),
        unfinished=("synchronize wiki",) if pending else (),
        publication_id=prepared.publication_id,
        projection_pending=pending,
    )


def confirm_stored_empty(scope, source_id, *, generation, context=None):
    base = scope.knowledge
    context = context or ExecutionContext()
    if context.storage_identity not in (None, base.identity):
        raise ValueError("Empty confirmation belongs to another database generation")
    context.storage_identity = base.identity
    if type(generation) is not int or not 0 <= generation < 2**63:
        raise ValueError("Empty confirmation requires the reviewed source generation")
    catalog, store = (
        CatalogStore(base.root, base.identity),
        KnowledgeStore(base.root, base.identity),
    )
    source = catalog.source(source_id)
    if source.contribution_empty and not source.removed and source.generation == generation + 1:
        retained = committed_source_change(
            store, base, source_id, kind="empty", source_generation=generation
        )
        if retained is not None and retained[1].source_revision_id == source.revision_id:
            projection = sync_publication(base.root, base.identity, retained[0])
            return _completed(scope, retained[0], projection)
    if (
        source.removed
        or source.contribution_empty
        or source.generation != generation
        or store.bind(view_id=scope.view_id).publication_id != base.publication_id
    ):
        return RefreshResult("conflict", scope.view_id, message="Source or knowledge changed")
    claim = SourceChange(
        source_id=source_id,
        source_revision_id=source.revision_id,
        source_generation=generation,
        kind="empty",
    )
    result = None
    try:
        with begin_storage_operation(base.root, context, "confirm-empty-source"):
            with task_stage(
                context, "source.empty.v1", root=base.root, source_revision_id=source.revision_id
            ):
                try:
                    outcome = publish_knowledge_changes(
                        base,
                        {},
                        source_change=claim,
                        check_cancelled=context.check_stop,
                        execution_id=context.storage_execution.execution_id,
                    )
                except Exception:
                    retained = committed_source_change(store, base, source_id, kind="empty")
                    if retained is None:
                        raise
                    prepared, _ = retained
                    projection = sync_publication(base.root, base.identity, prepared)
                else:
                    if outcome.status != "published":
                        return RefreshResult(
                            "conflict", scope.view_id, message="Source or knowledge changed"
                        )
                    prepared, projection = outcome.publication, outcome.projection
                result = _completed(scope, prepared, projection)
                context._storage_result = "completed"
    except Exception as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        return RefreshResult(
            "stopped"
            if isinstance(error, LockCancelled)
            else "conflict"
            if isinstance(error, (CatalogConflict, PublicationConflict))
            else "failed",
            scope.view_id,
            message=failure_reason(error),
            unfinished=("empty_confirmation",),
        )
    return result
