"""Review and explicitly accept retained compiler changes without model work."""

import difflib
from contextlib import nullcontext
from dataclasses import replace

from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
from ultrakb.locks import LockCancelled
from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
from ultrakb.storage.import_candidates import ImportCandidates
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict

from .execution import ExecutionContext, current_execution
from .proposals import ProposalPage, ProposalView
from .publications import sync_publication
from .storage_execution import begin_storage_operation
from .task_stages import task_stage


def _candidate(scope, proposal_id):
    store = ImportCandidates(scope.kb_dir, scope.knowledge.identity)
    candidate = store.read(proposal_id)
    if candidate["view_id"] != scope.view_id:
        raise ValueError("The import candidate belongs to another knowledge view")
    return store, candidate


def _view(candidate, diff=""):
    return ProposalView(
        candidate["proposal_id"],
        candidate["source_id"],
        candidate["unit_id"],
        candidate["view_id"],
        "completed" if candidate["status"] == "accepted" else candidate["status"],
        candidate["version"],
        candidate["conflicts"],
        diff,
    )


def read_stored_proposal(scope, proposal_id):
    store, candidate = _candidate(scope, proposal_id)
    before = KnowledgeStore(scope.kb_dir, scope.knowledge.identity).bind(
        candidate["prepared"].expected_publication, view_id=scope.view_id
    )
    old, new, chunks = before.files(), store.files(proposal_id), []
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) == new.get(path):
            continue
        if not path.endswith((".md", ".json", ".txt")):
            chunks.append(f"Asset changed: {path}\n")
            continue
        left = before.read(path).decode("utf-8") if path in old else ""
        right = store.read_file(proposal_id, path).decode("utf-8") if path in new else ""
        chunks.extend(
            difflib.unified_diff(
                left.splitlines(True),
                right.splitlines(True),
                fromfile=f"before/{path}",
                tofile=f"after/{path}",
            )
        )
    return _view(candidate, "".join(chunks))


def list_stored_proposals(scope):
    items, after = [], None
    while True:
        page = page_stored_proposals(scope, after=after)
        items.extend(item for item in page.proposals if item.status == "awaiting_confirmation")
        if page.next_after is None:
            return tuple(items)
        after = page.next_after


def page_stored_proposals(scope, *, after=None, limit=63):
    items, next_after = ImportCandidates(scope.kb_dir, scope.knowledge.identity).summaries(
        scope.view_id, after=after, limit=limit
    )
    return ProposalPage(tuple(_view(item) for item in items), next_after)


def _result(scope, candidate, *, status, unfinished=(), message=None):
    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    unit = catalog.unit(candidate["unit_id"])
    outcome = KnowledgeStore(scope.kb_dir, scope.knowledge.identity).compilation_outcome(
        candidate["proposal_id"]
    )
    accepted = status in {"added", "skipped"}
    return IngestResult(
        unit.doc_name,
        status,
        ("wiki",) if accepted else (),
        source_id=candidate["source_id"],
        source_revision_id=candidate["source_revision_id"],
        quality=outcome.quality,
        publication_id=candidate["prepared"].publication_id if accepted else None,
        projection_pending=accepted and "synchronize wiki" in unfinished,
        unfinished=unfinished,
        message=message,
        units=(
            ImportUnitOutcome(
                candidate["unit_id"],
                "completed" if accepted else "awaiting_confirmation",
                candidate["unit_revision_id"],
                successful_revision_id=candidate["unit_revision_id"] if accepted else None,
                successful_source_revision_id=candidate["source_revision_id"] if accepted else None,
                knowledge_revision_id=candidate["proposal_id"] if accepted else None,
                view_id=scope.view_id,
                proposal_id=candidate["proposal_id"],
                doc_name=unit.doc_name,
                quality=outcome.quality,
                unfinished=unfinished,
            ),
        ),
    )


def _publish(scope, candidate, context):
    root, identity = scope.kb_dir, scope.knowledge.identity
    prepared, publication = candidate["prepared"], None
    observed = current_execution() is context
    # Prepare presentation before publishing. Later metadata I/O cannot erase a
    # receipt that the publication boundary has already returned.
    result = _result(
        scope,
        candidate,
        status="skipped" if candidate["status"] == "accepted" else "added",
        unfinished=("synchronize wiki",),
    )
    store = KnowledgeStore(root, identity)
    try:
        with (
            task_stage(context, "import.review.accept.v1", root=root) if observed else nullcontext()
        ):
            try:
                publication = store.publish(
                    prepared,
                    review_version=candidate["version"],
                    execution_id=context.storage_execution.execution_id if observed else None,
                    check_cancelled=context.check_stop,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
            context._storage_result = "completed"
    except Exception:
        if publication is None:
            raise
        result = replace(result, unfinished=(*result.unfinished, "confirm publication observation"))
    try:
        with task_stage(context, "wiki.sync.v1", root=root) if observed else nullcontext():
            projection = sync_publication(root, identity, prepared)
        if projection.status in {"synced", "obsolete"}:
            result = replace(
                result,
                unfinished=tuple(item for item in result.unfinished if item != "synchronize wiki"),
                projection_pending=False,
            )
    except Exception:
        pass
    return result


def accept_stored_proposal(scope, proposal_id, *, version, context=None):
    candidates, candidate = _candidate(scope, proposal_id)
    if candidate["version"] != version or candidate["status"] == "superseded":
        return _result(
            scope,
            candidate,
            status="blocked",
            unfinished=("import_review",),
            message="The input, publication or reviewed candidate changed; "
            "the candidate is retained",
        )
    context = context or ExecutionContext()
    if context.storage_identity not in (None, scope.knowledge.identity):
        raise ValueError("Import acceptance belongs to another database generation")
    context.storage_identity = scope.knowledge.identity
    if candidate["status"] == "accepted":
        return _publish(scope, candidate, context)
    result = None
    try:
        with begin_storage_operation(scope.kb_dir, context, "accept-import-review"):
            if not candidate["sync_ready"]:
                with task_stage(context, "import.review.prepare.v1", root=scope.kb_dir):
                    refreshed = candidates.reprepare_sync(candidate, context.storage_execution)
                    result = _result(
                        scope,
                        refreshed,
                        status="blocked",
                        unfinished=("import_review",),
                        message="The working-copy baseline changed. Review the freshly prepared "
                        "candidate; completed compiler work was reused.",
                    )
                    context._storage_result = "completed"
            else:
                result = _publish(scope, candidate, context)
    except Exception as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        return _result(
            scope,
            candidate,
            status="stopped"
            if isinstance(error, LockCancelled)
            else "blocked"
            if isinstance(error, (CatalogConflict, PublicationConflict))
            else "failed",
            unfinished=("import_review",),
            message=str(error),
        )
    return result
