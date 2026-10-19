"""Shared development use case for committing an import and projecting its Wiki."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultrakb.storage.catalog_store import CatalogConflict
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.knowledge_store import (
    BoundKnowledge,
    KnowledgeStore,
    PreparedRevision,
    Publication,
    PublicationConflict,
)
from ultrakb.storage.pending_store import PendingWork
from ultrakb.storage.wiki_projection import ProjectionConflict, ProjectionResult, ProjectionStore


@dataclass(frozen=True)
class PublicationOutcome:
    status: Literal["published", "needs_review"]
    publication: Publication | None
    projection: ProjectionResult | None
    candidate_revision_id: str | None
    reason_code: str | None = None


def publish_import_result(
    root: Path,
    identity: DatabaseIdentity,
    files: dict[str, bytes],
    *,
    work: PendingWork,
    expected: str | None,
    view_id: str = "legacy",
    unit_revisions: tuple[str, ...] = (),
    index_revisions: tuple[str, ...] = (),
) -> PublicationOutcome:
    knowledge = KnowledgeStore(root, identity)
    prepared = None
    try:
        prepared = knowledge.committed_import(work, view_id=view_id)
        if prepared is None:
            prepared = knowledge.prepare_revision(
                files,
                expected=expected,
                view_id=view_id,
                unit_revisions=unit_revisions,
                index_revisions=index_revisions,
                import_work=work,
            )
        publication = knowledge.publish(prepared)
    except (PublicationConflict, CatalogConflict, ProjectionConflict) as error:
        # Another caller may have committed this exact attempt after our first lookup.
        committed = knowledge.committed_import(work, view_id=view_id)
        if committed is None:
            reason = {
                PublicationConflict: "knowledge_changed",
                CatalogConflict: "import_changed",
                ProjectionConflict: "worktree_baseline_changed",
            }[type(error)]
            return PublicationOutcome(
                "needs_review", None, None, prepared.revision_id if prepared else None, reason
            )
        prepared = committed
        publication = knowledge.publish(prepared)
    projection = sync_publication(root, identity, prepared)
    return PublicationOutcome("published", publication, projection, prepared.revision_id)


def publish_knowledge_changes(
    base: BoundKnowledge,
    changes: dict[str, bytes | None],
    *,
    page_inputs: dict[str, tuple[str, ...]] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    execution_id: str | None = None,
    withdrawal=None,
) -> PublicationOutcome:
    """Commit an explicitly based manual edit before attempting worktree I/O."""
    knowledge = KnowledgeStore(base.root, base.identity)
    prepared = knowledge.prepare_changes(
        base, changes, page_inputs=page_inputs, withdrawal=withdrawal
    )
    try:
        publication = knowledge.publish(
            prepared, check_cancelled=check_cancelled, execution_id=execution_id
        )
    except (PublicationConflict, ProjectionConflict, CatalogConflict) as error:
        return PublicationOutcome(
            "needs_review",
            None,
            None,
            prepared.revision_id,
            "knowledge_changed"
            if isinstance(error, PublicationConflict)
            else "source_changed"
            if isinstance(error, CatalogConflict)
            else "worktree_baseline_changed",
        )
    except Exception:
        committed = knowledge.committed_publication(prepared)
        if committed is None:
            raise
        publication = committed
    projection = sync_publication(base.root, base.identity, prepared)
    return PublicationOutcome("published", publication, projection, prepared.revision_id)


def sync_publication(
    root: Path, identity: DatabaseIdentity, prepared: PreparedRevision
) -> ProjectionResult:
    try:
        return ProjectionStore(root, identity, view_id=prepared.view_id).sync(
            plan_id=prepared.sync_plan_id
        )
    except Exception as error:
        # The master commit succeeded even if coordination or opening the projection failed.
        return ProjectionResult("pending", prepared.sync_plan_id, error_code=type(error).__name__)
