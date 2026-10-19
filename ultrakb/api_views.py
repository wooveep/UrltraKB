"""Explicit view selection shared by HTTP read and write adapters."""

from pathlib import Path
from threading import Event

from fastapi import HTTPException
from pydantic import BaseModel

from ultrakb.application.views import view_scope
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.source_records import RecordId, ViewId


class ViewRequest(BaseModel):
    view_id: ViewId | None = None


class PublishedReadRequest(ViewRequest):
    publication_id: RecordId | None = None


async def resolve_api_scope(root: Path, view_id: str | None) -> KnowledgeScope:
    try:
        from ultrakb.context.workers import run_blocking

        stopped = Event()
        return await run_blocking(
            lambda: view_scope(root, view_id or "legacy", cancelled=stopped.is_set),
            on_cancel=stopped.set,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Unknown knowledge view") from exc


async def resolve_api_read_scope(
    root: Path,
    view_id: str | None,
    publication_id: str | None = None,
    *,
    knowledge_revision_id: str | None = None,
) -> KnowledgeScope:
    """Bind related HTTP reads to one public identity or explicit historical revision."""
    from ultrakb.application.views import published_scope
    from ultrakb.context.workers import run_blocking

    stopped = Event()

    def bind():
        if knowledge_revision_id is not None:
            if publication_id is not None:
                scope = published_scope(
                    root,
                    view_id or "legacy",
                    publication_id=publication_id,
                    cancelled=stopped.is_set,
                )
                if scope.knowledge.publication.revision_id != knowledge_revision_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Public identity and knowledge revision do not match",
                    )
                return scope
            return view_scope(
                root,
                view_id or "legacy",
                historical_revision=knowledge_revision_id,
                cancelled=stopped.is_set,
            )
        return published_scope(
            root,
            view_id or "legacy",
            publication_id=publication_id,
            cancelled=stopped.is_set,
        )

    try:
        return await run_blocking(bind, on_cancel=stopped.set)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404, detail="Requested knowledge snapshot unavailable"
        ) from exc
