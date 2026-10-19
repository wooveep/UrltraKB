"""Document REST endpoints: read a document's ingested source text.

An APIRouter (sibling of api_pages_router.py) so api.py stays under the
per-file line gate. Read-only: source documents are ``Do not modify directly``
artifacts, so there is no edit/delete counterpart here (document removal lives
on ``/api/v1/remove``).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.api_models import DocumentSourceRequest, DocumentSourceResponse
from ultrakb.api_views import ViewRequest, resolve_api_read_scope, resolve_api_scope
from ultrakb.documents import read_document_source
from ultrakb.source_pages import PageRangeError

documents_router = APIRouter()


class ProposalRequest(ViewRequest):
    kb: str = "default"
    proposal_id: str


class ProposalAcceptRequest(ProposalRequest):
    version: str


@documents_router.get("/api/v1/proposals")
async def proposals_endpoint(
    response: Response,
    kb: str = "default",
    view_id: str | None = None,
    after: str | None = None,
    limit: int = Query(default=63, ge=1, le=128),
    _: None = Depends(require_bearer_token),
):
    from ultrakb.application.proposals import list_proposal_page

    root = await asyncio.to_thread(_resolve_kb, kb)
    scope = await resolve_api_scope(root, view_id)
    try:
        page = await run_in_threadpool(
            list_proposal_page, root, scope=scope, after=after, limit=limit
        )
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail="Unknown proposal cursor or page") from error
    if page.next_after is not None:
        response.headers["X-UltraKB-Next-After"] = page.next_after
    return [asdict(view) for view in page.proposals]


@documents_router.post("/api/v1/proposal")
async def proposal_endpoint(request: ProposalRequest, _: None = Depends(require_bearer_token)):
    from ultrakb.application.proposals import read_proposal

    root = await asyncio.to_thread(_resolve_kb, request.kb)
    try:
        return asdict(
            await run_in_threadpool(
                read_proposal,
                root,
                request.proposal_id,
                scope=await resolve_api_scope(root, request.view_id),
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Proposal is unavailable or changed") from exc


@documents_router.post("/api/v1/proposal/accept")
async def accept_proposal_endpoint(
    request: ProposalAcceptRequest, _: None = Depends(require_bearer_token)
):
    from ultrakb.application.proposals import accept_proposal

    root = await asyncio.to_thread(_resolve_kb, request.kb)
    try:
        return asdict(
            await run_in_threadpool(
                accept_proposal,
                root,
                request.proposal_id,
                version=request.version,
                scope=await resolve_api_scope(root, request.view_id),
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Proposal is unavailable or changed") from exc


@documents_router.post("/api/v1/document/source", response_model=DocumentSourceResponse)
async def document_source_endpoint(
    request: DocumentSourceRequest,
    _: None = Depends(require_bearer_token),
) -> DocumentSourceResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = await resolve_api_read_scope(
        kb_dir,
        request.view_id,
        request.publication_id,
        knowledge_revision_id=request.knowledge_revision_id,
    )
    try:
        result = await run_in_threadpool(
            read_document_source,
            kb_dir,
            request.hash,
            source_revision_id=request.source_revision_id,
            unit_id=request.unit_id,
            cells=request.cells,
            pages=request.pages,
            chars=request.chars,
            blocks=request.blocks,
            part=request.part,
            scope=scope,
        )
    except PageRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        # Corrupt/unreadable source file (bad JSON, unexpected shape, I/O error):
        # a controlled 500 with a clean message beats an unhandled stack trace.
        raise HTTPException(status_code=500, detail="Could not read document source.") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Document source not found.")
    return DocumentSourceResponse(**result)


class RetryWorksheetRequest(ViewRequest):
    kb: str
    source_id: str
    unit_id: str


class RetrySourceRequest(ViewRequest):
    kb: str
    source_id: str


@documents_router.post("/api/v1/document/retry")
async def retry_source_endpoint(
    request: RetrySourceRequest, _: None = Depends(require_bearer_token)
):
    from ultrakb.application.source_retry import retry_source

    root = await asyncio.to_thread(_resolve_kb, request.kb)
    try:
        return asdict(
            await run_in_threadpool(
                retry_source,
                root,
                request.source_id,
                scope=await resolve_api_scope(root, request.view_id),
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@documents_router.post("/api/v1/document/retry-worksheet")
async def retry_worksheet_endpoint(
    request: RetryWorksheetRequest, _: None = Depends(require_bearer_token)
):
    from ultrakb.application.workbook_actions import retry_worksheet

    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = await resolve_api_scope(kb_dir, request.view_id)
    try:
        return asdict(
            await run_in_threadpool(
                retry_worksheet, kb_dir, request.source_id, request.unit_id, scope=scope
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ReprocessPreviewRequest(ViewRequest):
    kb: str
    source_id: str


class ReprocessRequest(ReprocessPreviewRequest):
    version: str


@documents_router.post("/api/v1/document/reprocess/preview")
async def preview_reprocess_endpoint(
    request: ReprocessPreviewRequest, _: None = Depends(require_bearer_token)
):
    from ultrakb.application.reprocessing import preview_reprocessing

    root = await asyncio.to_thread(_resolve_kb, request.kb)
    try:
        return await run_in_threadpool(
            preview_reprocessing,
            root,
            request.source_id,
            scope=await resolve_api_scope(root, request.view_id),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@documents_router.post("/api/v1/document/reprocess")
async def reprocess_endpoint(request: ReprocessRequest, _: None = Depends(require_bearer_token)):
    from ultrakb.application.reprocessing import reprocess_source

    root = await asyncio.to_thread(_resolve_kb, request.kb)
    try:
        return asdict(
            await run_in_threadpool(
                reprocess_source,
                root,
                request.source_id,
                version=request.version,
                scope=await resolve_api_scope(root, request.view_id),
            )
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
