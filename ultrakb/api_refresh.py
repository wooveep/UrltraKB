"""HTTP adapters for explicit current-view refresh and protected differences."""

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.api_views import resolve_api_scope
from ultrakb.application import refresh as application
from ultrakb.source_records import Digest, RecordId, ViewId

refresh_router = APIRouter(dependencies=[Depends(require_bearer_token)])


class RefreshRequest(BaseModel):
    kb: str = "default"
    view_id: ViewId = "legacy"


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
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@refresh_router.get("/api/v1/refresh/status")
async def status(kb: str, view_id: ViewId = "legacy"):
    root, scope = await _selected(kb, view_id)
    return await asyncio.to_thread(application.refresh_status, root, scope=scope)


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


@refresh_router.get("/api/v1/refresh/references")
async def references(kb: str):
    from ultrakb.artifact_references import list_artifact_references

    root = await asyncio.to_thread(_resolve_kb, kb)
    found = await asyncio.to_thread(list_artifact_references, root)
    return {
        "complete": found.complete,
        "paths": [str(p) for p in sorted(found.paths)],
        "index_documents": sorted(found.index_documents),
    }


@refresh_router.post("/api/v1/refresh")
async def refresh(request: RefreshRequest):
    root, scope = await _selected(request.kb, request.view_id)
    return asdict(await application.refresh_knowledge_view(root, scope=scope))


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
async def proposals(kb: str, view_id: ViewId = "legacy"):
    root, scope = await _selected(kb, view_id)
    return await asyncio.to_thread(application.list_refresh_proposals, root, scope=scope)


@refresh_router.post("/api/v1/refresh/proposal")
async def proposal(request: ProposalRequest):
    root, scope = await _selected(request.kb, request.view_id)
    try:
        return await asyncio.to_thread(
            application.read_refresh_proposal, root, request.proposal_id, scope=scope
        )
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
