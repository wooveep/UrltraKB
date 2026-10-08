"""Explicit view selection shared by HTTP read and write adapters."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openkb.application.views import list_views, view_scope
from openkb.knowledge_scope import KnowledgeScope
from openkb.source_records import RecordId, ViewId


class ViewRequest(BaseModel):
    view_id: ViewId | None = None


class DefaultViewRequest(ViewRequest):
    kb: str = "default"
    family_id: RecordId


async def resolve_api_scope(
    root: Path, view_id: str | None, source_name: str | None = None
) -> KnowledgeScope | None:
    if source_name:
        from openkb.application.query_choices import source_query_scope

        if view_id:
            raise HTTPException(status_code=400, detail="Select either a source or a view")
        try:
            return await asyncio.to_thread(source_query_scope, root, source_name)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @router.get("/api/v1/versions/defaults")
    async def defaults(kb: str = "default", _: None = Depends(require_bearer_token)):
        from openkb.application.query_views import list_family_defaults

        root = await asyncio.to_thread(_resolve_kb, kb)
        return await asyncio.to_thread(list_family_defaults, root)

    @router.post("/api/v1/versions/default")
    async def select_default(request: DefaultViewRequest, _: None = Depends(require_bearer_token)):
        from openkb.application.query_views import select_default_view

        root = await asyncio.to_thread(_resolve_kb, request.kb)
        try:
            return await asyncio.to_thread(
                select_default_view, root, request.family_id, request.view_id
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
