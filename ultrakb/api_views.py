"""Explicit view selection shared by HTTP read and write adapters."""

import asyncio
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

from ultrakb.application.views import view_scope
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.source_records import RecordId, ViewId


class ViewRequest(BaseModel):
    view_id: ViewId | None = None


class PublishedReadRequest(ViewRequest):
    publication_id: RecordId | None = None


async def resolve_api_scope(root: Path, view_id: str | None) -> KnowledgeScope | None:
    if view_id is None:
        return None
    try:
        return await asyncio.to_thread(view_scope, root, view_id)
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
    from ultrakb.published_knowledge import PublicationUnavailable

    try:
        if knowledge_revision_id is not None:
            if publication_id is not None:
                scope = await asyncio.to_thread(
                    published_scope, root, view_id or "legacy", publication_id=publication_id
                )
                public = scope.publication
                if public is None or public.knowledge_revision_id != knowledge_revision_id:
                    raise PublicationUnavailable(
                        "Public identity and knowledge revision do not match"
                    )
                return scope
            return await asyncio.to_thread(
                view_scope, root, view_id or "legacy", historical_revision=knowledge_revision_id
            )
        return await asyncio.to_thread(
            published_scope, root, view_id or "legacy", publication_id=publication_id
        )
    except PublicationUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404, detail="Requested knowledge snapshot unavailable"
        ) from exc
