"""HTTP adapters for explicit current-view refresh and protected differences."""

import asyncio
from dataclasses import asdict
from threading import Event

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.api_views import resolve_api_scope
from ultrakb.application import refresh as application
from ultrakb.context.workers import run_blocking
from ultrakb.source_records import Digest, RecordId, RelativePath, ViewId

refresh_router = APIRouter(dependencies=[Depends(require_bearer_token)])


class RefreshRequest(BaseModel):
    kb: str = "default"
    view_id: ViewId = "legacy"
    source_ids: list[RecordId] = Field(default_factory=list)
    page_paths: list[RelativePath] | None = None


class EmptyRequest(RefreshRequest):
    source_id: RecordId
    generation: int = Field(ge=1)


class ProposalRequest(RefreshRequest):
    proposal_id: RecordId


class AcceptRequest(ProposalRequest):
    version: Digest


async def _selected(kb, view_id):
    root = await asyncio.to_thread(_resolve_kb, kb)
    return root, await resolve_api_scope(root, view_id)


async def _history_read(operation, *args, **kwargs):
    stopped = Event()
    scope = kwargs.get("scope")

    def read():
        if scope is not None and scope.knowledge is not None:
            from ultrakb.storage.coordination import storage_stage

            with storage_stage(
                scope.kb_dir, scope.knowledge.identity, read_only=True, cancelled=stopped.is_set
            ):
                return operation(*args, **kwargs)
        return operation(*args, **kwargs)

    try:
        return await run_blocking(read, on_cancel=stopped.set)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@refresh_router.get("/api/v1/refresh/status")
async def status(kb: str, view_id: ViewId = "legacy"):
    root, scope = await _selected(kb, view_id)
    return await _history_read(application.refresh_status, root, scope=scope)


@refresh_router.get("/api/v1/refresh/history")
async def history(
    kb: str,
    view_id: ViewId = "legacy",
    after: RecordId | None = None,
    limit: int = Query(default=63, ge=1, le=128),
    publication_id: RecordId | None = None,
):
    root, scope = await _selected(kb, view_id)
    return await _history_read(
        application.list_knowledge_history,
        root,
        scope=scope,
        after=after,
        limit=limit,
        publication_id=publication_id,
    )


@refresh_router.get("/api/v1/refresh/history/pages")
async def history_pages(
    kb: str,
    knowledge_revision_id: RecordId,
    view_id: ViewId = "legacy",
    after: str | None = None,
    limit: int = Query(default=63, ge=1, le=128),
):
    from ultrakb.application.knowledge_history import history_pages

    root, scope = await _selected(kb, view_id)
    return await _history_read(
        history_pages, root, knowledge_revision_id, scope=scope, after=after, limit=limit
    )


@refresh_router.get("/api/v1/refresh/history/page")
async def history_page(
    kb: str, knowledge_revision_id: RecordId, path: str, view_id: ViewId = "legacy"
):
    from ultrakb.application.knowledge_history import read_history_page

    root, scope = await _selected(kb, view_id)
    page, _ = await _history_read(read_history_page, root, knowledge_revision_id, path, scope=scope)
    return asdict(page)


@refresh_router.post("/api/v1/refresh")
async def refresh(request: RefreshRequest):
    root, scope = await _selected(request.kb, request.view_id)
    if request.page_paths is not None and not request.source_ids:
        raise HTTPException(status_code=400, detail="Page selection requires source identities")
    return asdict(
        await application.refresh_selected_sources(
            root, request.source_ids, scope=scope, page_paths=request.page_paths
        )
        if request.source_ids
        else await application.refresh_knowledge_view(root, scope=scope)
    )


@refresh_router.post("/api/v1/refresh/empty")
async def empty(request: EmptyRequest):
    root, scope = await _selected(request.kb, request.view_id)
    try:
        return asdict(
            await asyncio.to_thread(
                application.confirm_empty_source,
                root,
                request.source_id,
                generation=request.generation,
                scope=scope,
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@refresh_router.get("/api/v1/refresh/proposals")
async def proposals(
    kb: str,
    view_id: ViewId = "legacy",
    after: RecordId | None = None,
    limit: int = Query(default=63, ge=1, le=128),
):
    root, scope = await _selected(kb, view_id)
    page = await _history_read(
        application.list_refresh_proposal_page, root, scope=scope, after=after, limit=limit
    )
    return JSONResponse(
        list(page.proposals),
        headers={"X-UltraKB-Next-After": page.next_after} if page.next_after else {},
    )


@refresh_router.post("/api/v1/refresh/proposal")
async def proposal(request: ProposalRequest):
    root, scope = await _selected(request.kb, request.view_id)
    try:
        return await _history_read(
            application.read_refresh_proposal, root, request.proposal_id, scope=scope
        )
    except HTTPException as exc:
        if exc.status_code in {400, 404}:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@refresh_router.post("/api/v1/refresh/accept")
async def accept(request: AcceptRequest):
    root, scope = await _selected(request.kb, request.view_id)
    try:
        return asdict(
            await asyncio.to_thread(
                application.accept_refresh_proposal,
                root,
                request.proposal_id,
                version=request.version,
                scope=scope,
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
