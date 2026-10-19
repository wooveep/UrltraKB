"""Publish a generator's exact retained exploration candidate before projection."""

from contextlib import nullcontext
from dataclasses import replace

from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.catalog_store import CatalogConflict
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.wiki_projection import ProjectionConflict

from .publications import sync_publication
from .task_stages import task_stage


def accept_generation_knowledge(scope, plan, result, context, *, observed):
    root, prepared = scope.kb_dir, plan.prepared
    store = KnowledgeStore(root, scope.knowledge.identity)
    publication = None
    try:
        with task_stage(context, "knowledge.publish.v1", root=root) if observed else nullcontext():
            try:
                publication = store.publish(
                    prepared,
                    execution_id=context.storage_execution.execution_id if observed else None,
                    check_cancelled=context.check_stop,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
    except Exception as error:
        if publication is None:
            return replace(
                result,
                status="conflict"
                if isinstance(error, (PublicationConflict, ProjectionConflict, CatalogConflict))
                else "blocked"
                if isinstance(error, RecoveryRequired)
                else "failed",
                candidate_revision_id=prepared.revision_id,
                error_type=type(error).__name__,
                unfinished=(*result.unfinished, "publish generated notes"),
            )
        result = replace(result, unfinished=(*result.unfinished, "confirm publication observation"))
    writes = tuple(
        str(root / "wiki" / path)
        for path, operation in plan.details.knowledge_changes
        if operation == "write"
    )
    archive = plan.details.archive_path
    archive_saved = (
        archive is not None
        and plan.details.output_dir == archive
        and any(
            path.startswith(archive + "/")
            for path in ("wiki/" + item[0] for item in plan.details.knowledge_changes)
        )
    )
    result = replace(
        result,
        publication_id=publication.publication_id,
        candidate_revision_id=prepared.revision_id,
        resources=(*result.resources, *writes, *((str(root / archive),) if archive_saved else ())),
        changes=(
            *result.changes,
            *(
                f"{'removed' if operation == 'delete' else 'saved'}: wiki/{path}"
                for path, operation in plan.details.knowledge_changes
            ),
        ),
        archive_path=root / archive if archive_saved else result.archive_path,
        projection_pending=True,
    )
    try:
        with task_stage(context, "wiki.sync.v1", root=root) if observed else nullcontext():
            projection = sync_publication(root, scope.knowledge.identity, prepared)
        if projection.status in {"synced", "obsolete"}:
            return replace(result, projection_pending=False)
    except Exception:
        pass
    return replace(result, unfinished=(*result.unfinished, "synchronize wiki"))
