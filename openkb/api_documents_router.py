"""Document REST endpoints: read a document's ingested source text.

An APIRouter (sibling of api_pages_router.py) so api.py stays under the
per-file line gate. Read-only: source documents are ``Do not modify directly``
artifacts, so there is no edit/delete counterpart here (document removal lives
on ``/api/v1/remove``).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_models import DocumentSourceRequest, DocumentSourceResponse
from openkb.api_views import ViewRequest, resolve_api_scope
from openkb.documents import read_document_source
from openkb.source_pages import PageRangeError

documents_router = APIRouter()


class ProposalRequest(ViewRequest):
    kb: str = "default"
    proposal_id: str


class ProposalAcceptRequest(ProposalRequest):
    version: str


@documents_router.get("/api/v1/proposals")
async def proposals_endpoint(
    kb: str = "default", view_id: str | None = None, _: None = Depends(require_bearer_token)
):
    from openkb.application.proposals import list_proposals

    root = await asyncio.to_thread(_resolve_kb, kb)
    scope = await resolve_api_scope(root, view_id)
    return [asdict(view) for view in await run_in_threadpool(list_proposals, root, scope=scope)]


@documents_router.post("/api/v1/proposal")
async def proposal_endpoint(request: ProposalRequest, _: None = Depends(require_bearer_token)):
    from openkb.application.proposals import read_proposal

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
    from openkb.application.proposals import accept_proposal

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
    scope = await resolve_api_scope(kb_dir, request.view_id)
    if request.knowledge_revision_id:
        from openkb.application.views import view_scope

        try:
            scope = await asyncio.to_thread(
                view_scope,
                kb_dir,
                request.view_id or "legacy",
                historical_revision=request.knowledge_revision_id,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Knowledge revision unavailable.") from exc
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


@documents_router.post("/api/v1/document/retry-worksheet")
async def retry_worksheet_endpoint(
    request: RetryWorksheetRequest, _: None = Depends(require_bearer_token)
):
    from openkb.application.workbook_actions import retry_worksheet

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
