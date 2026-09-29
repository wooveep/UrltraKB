"""Explicit view selection shared by HTTP read and write adapters."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openkb.application.views import list_views, view_scope
from openkb.knowledge_scope import KnowledgeScope
from openkb.source_records import ViewId


class ViewRequest(BaseModel):
    view_id: ViewId | None = None


async def resolve_api_scope(root: Path, view_id: str | None) -> KnowledgeScope | None:
    if view_id is None:
        return None
    try:
        return await asyncio.to_thread(view_scope, root, view_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Unknown knowledge view") from exc


def make_views_router() -> APIRouter:
    from openkb.api_helpers import _resolve_kb, require_bearer_token

    router = APIRouter()

    @router.get("/api/v1/views")
    async def views(kb: str, _: None = Depends(require_bearer_token)):
        root = await asyncio.to_thread(_resolve_kb, kb)
        return [view.model_dump(mode="json") for view in await asyncio.to_thread(list_views, root)]

    @router.post("/api/v1/views/map-legacy")
    async def map_legacy(kb: str, _: None = Depends(require_bearer_token)):
        from openkb.application.views import map_legacy_sources

        root = await asyncio.to_thread(_resolve_kb, kb)
        return await asyncio.to_thread(map_legacy_sources, root)

    return router
