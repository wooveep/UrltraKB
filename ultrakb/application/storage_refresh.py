"""Shared main-store refresh generation, explicit review and exact publication recovery."""

import asyncio
import difflib
from contextlib import nullcontext
from dataclasses import replace

from ultrakb.context.workers import run_blocking
from ultrakb.ingest_diagnostics import failure_reason
from ultrakb.locks import LockCancelled
from ultrakb.refresh_publication import RefreshResult
from ultrakb.storage.catalog_store import CatalogConflict
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.refresh_store import RefreshStore, refresh_targets
from ultrakb.storage.wiki_projection import ProjectionConflict

from .execution import ExecutionContext, current_execution
from .publications import sync_publication
from .storage_execution import begin_storage_operation
from .storage_refresh_generation import prepare_refresh
from .task_stages import task_stage


def _context(scope, context):
    context = context or ExecutionContext()
    if context.storage_identity not in (None, scope.knowledge.identity):
        raise ValueError("Refresh belongs to another database generation")
    context.storage_identity = scope.knowledge.identity
    return context


def _review(scope, proposal):
    return RefreshResult(
        "conflict" if proposal["status"] == "superseded" else "awaiting_confirmation",
        scope.view_id,
        proposal_id=proposal["proposal_id"],
        message="Review retained changes before publishing; current knowledge is preserved.",
        unfinished=("refresh_review",),
    )


def _publish(scope, proposal, context):
    base, prepared = scope.knowledge, proposal["prepared"]
    store = KnowledgeStore(base.root, base.identity)
    publication = None
    observed = current_execution() is context and proposal["status"] != "accepted"
    unconfirmed = ()
    try:
        with (
            task_stage(context, "knowledge.publish.v1", root=base.root)
            if observed
            else nullcontext()
        ):
            try:
                publication = store.publish(
                    prepared,
                    check_cancelled=context.check_stop,
                    execution_id=context.storage_execution.execution_id
                    if context.storage_execution is not None
                    else None,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
            context._storage_result = "completed"
    except Exception:
        if publication is None:
            raise
        unconfirmed = ("confirm publication observation",)
    result = RefreshResult(
        "completed",
        scope.view_id,
        publication.revision_id,
        resources=("wiki",),
        publication_id=publication.publication_id,
        projection_pending=True,
        unfinished=("synchronize wiki", *unconfirmed),
    )
    try:
        with task_stage(context, "wiki.sync.v1", root=base.root) if observed else nullcontext():
            projection = sync_publication(base.root, base.identity, prepared)
    except Exception:
        return result
    if projection.status in {"synced", "obsolete"}:
        result = replace(result, projection_pending=False, unfinished=unconfirmed)
    return result


async def refresh_stored_view(scope, context=None):
    base = scope.knowledge
    context = _context(scope, context)
    store = RefreshStore(base.root, base.identity)
    if context.storage_execution is not None:
        previous = store.find(base.publication_id, context.storage_execution.model_budget_id)
        if previous is not None and previous["status"] == "accepted":
            return _publish(scope, previous, context)
    try:
        targets = refresh_targets(base)
    except PublicationConflict as error:
        return RefreshResult("conflict", scope.view_id, message=failure_reason(error))
    if targets.blocked:
        return RefreshResult(
            "blocked",
            scope.view_id,
            message="Finish importing current sources before refreshing: "
            + ", ".join(targets.blocked),
            unfinished=("current_source_import",),
        )
    result = None

    def worker(bundle):
        async def execute():
            nonlocal result
            previous = store.find(base.publication_id, context.storage_execution.model_budget_id)
            if (
                previous is not None
                and previous["status"] != "accepted"
                and previous["content_current"]
                and not previous["sync_ready"]
            ):
                previous = None  # Rebuild the plan from retained compiler steps, without resends.
            if previous is None:
                prepared, protected = await prepare_refresh(scope, targets, context, bundle)
                previous = store.save(
                    prepared, conflicts=protected, execution=context.storage_execution
                )
            if previous["status"] == "superseded" or previous["conflicts"]:
                result = _review(scope, previous)
                if result.status == "awaiting_confirmation":
                    context._storage_result = "completed"
            else:
                try:
                    result = _publish(scope, previous, context)
                except (CatalogConflict, PublicationConflict, ProjectionConflict):
                    result = _review(scope, store.read(previous["proposal_id"]))
            if result.status != "completed":
                context._storage_result = (
                    "completed" if result.status == "awaiting_confirmation" else "failed"
                )

        asyncio.run(execute())

    try:
        if targets.units:
            with context.begin(base.root) as bundle:
                await run_blocking(lambda: worker(bundle))
        else:
            with begin_storage_operation(base.root, context, "refresh-knowledge"):
                await run_blocking(lambda: worker(None))
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        return RefreshResult(
            "stopped"
            if isinstance(error, (LockCancelled, asyncio.CancelledError))
            else "conflict"
            if isinstance(error, (CatalogConflict, PublicationConflict))
            else "failed",
            scope.view_id,
            message=failure_reason(error),
            unfinished=("refresh",),
        )
    return result


def read_stored_refresh(scope, proposal_id):
    base = scope.knowledge
    store = RefreshStore(base.root, base.identity)
    proposal = store.read(proposal_id)
    if proposal["view_id"] != scope.view_id:
        raise ValueError("Refresh proposal belongs to another view")
    prepared = proposal["prepared"]
    before = KnowledgeStore(base.root, base.identity).bind(
        prepared.expected_publication, view_id=scope.view_id
    )
    old, new = before.files(), store.files(proposal_id)
    chunks = []
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) == new.get(path):
            continue
        if not path.endswith((".md", ".txt", ".json")):
            chunk = f"Asset changed: {path}\n"
        else:
            left = before.read(path).decode("utf-8") if path in old else ""
            right = store.read_file(proposal_id, path).decode("utf-8") if path in new else ""
            chunk = "".join(
                difflib.unified_diff(
                    left.splitlines(True),
                    right.splitlines(True),
                    fromfile=f"before/{path}",
                    tofile=f"after/{path}",
                )
            )
        chunks.append(chunk)
    return {
        key: value
        for key, value in proposal.items()
        if key not in {"prepared", "catalog_generation", "content_current", "sync_ready"}
    } | {"diff": "".join(chunks)}


def list_stored_refreshes(scope):
    return RefreshStore(scope.kb_dir, scope.knowledge.identity).summaries(scope.view_id)


def accept_stored_refresh(scope, proposal_id, *, version, context=None):
    context = _context(scope, context)
    proposal = RefreshStore(scope.kb_dir, scope.knowledge.identity).read(proposal_id)
    if proposal["view_id"] != scope.view_id:
        raise ValueError("Refresh proposal belongs to another view")
    if proposal["version"] != version or proposal["status"] == "superseded":
        return RefreshResult(
            "conflict",
            scope.view_id,
            proposal_id=proposal_id,
            message="Sources, knowledge or the reviewed candidate changed",
        )
    result = None
    try:
        with begin_storage_operation(scope.kb_dir, context, "accept-refresh"):
            with task_stage(context, "refresh.accept.v1", root=scope.kb_dir):
                result = _publish(scope, proposal, context)
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        return RefreshResult(
            "stopped"
            if isinstance(error, (LockCancelled, asyncio.CancelledError))
            else "conflict"
            if isinstance(error, (CatalogConflict, PublicationConflict))
            else "failed",
            scope.view_id,
            proposal_id=proposal_id,
            message=failure_reason(error),
            unfinished=("refresh_review",),
        )
    return result
