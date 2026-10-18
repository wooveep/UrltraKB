"""Shared development use case for committing an import and projecting its Wiki."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultrakb.storage.catalog_store import CatalogConflict
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.knowledge_store import KnowledgeStore, Publication, PublicationConflict
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
    try:
        projection = ProjectionStore(root, identity, view_id=view_id).sync(
            plan_id=prepared.sync_plan_id
        )
    except (OSError, sqlite3.Error) as error:
        # The master commit succeeded even if coordination or opening the projection failed.
        projection = ProjectionResult(
            "pending", prepared.sync_plan_id, error_code=type(error).__name__
        )
    return PublicationOutcome("published", publication, projection, prepared.revision_id)
