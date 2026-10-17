"""Wiki-page REST endpoints: read / delete a page.

An APIRouter (sibling of api_graph.py / api_config_router.py / api_kbs_router.py)
so api.py stays under the per-file line gate (tests/test_file_size.py). All
endpoints depend only on module-level helpers (_resolve_kb, require_bearer_token)
and page_ops — no create_app closure — so they extract cleanly. Page mutations
serialize via page_ops' own KB ingest lock, not the app's per-KB asyncio lock.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.api_models import (
    PageDeleteRequest,
    PageDeleteResponse,
    PageEditRequest,
    PageEditResponse,
    PageLinksRequest,
    PageLinksResponse,
    PageRequest,
    PageResponse,
)
from ultrakb.api_views import resolve_api_read_scope, resolve_api_scope
from ultrakb.application.pages import delete_page, read_page, save_page

pages_router = APIRouter()


@pages_router.get("/api/v1/image")
async def image_endpoint(
    kb: str = Query(...),
    path: str = Query(..., min_length=1),
    publication_id: str | None = Query(None),
    view_id: str | None = Query(None),
    _: None = Depends(require_bearer_token),
) -> Response:
    from ultrakb.application.reading import read_knowledge_image

    root = await asyncio.to_thread(_resolve_kb, kb)
    scope = await resolve_api_read_scope(root, view_id, publication_id)
    try:
        data, media_type = await run_in_threadpool(read_knowledge_image, root, path, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Knowledge image unavailable") from exc
    return Response(
        data,
        media_type=media_type,
        headers={"X-UltraKB-Publication-ID": scope.publication_id or ""},
    )


@pages_router.post("/api/v1/page", response_model=PageResponse)
async def page_endpoint(
    request: PageRequest,
    _: None = Depends(require_bearer_token),
) -> PageResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = await resolve_api_read_scope(
        kb_dir,
        request.view_id,
        request.publication_id,
        knowledge_revision_id=request.knowledge_revision_id,
    )
    try:
        page = await run_in_threadpool(read_page, kb_dir, request.path, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid page path.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Page not found: {request.path}") from exc
    return PageResponse(
        publication_id=page.publication_id,
        version=page.version,
        path=request.path,
        content=page.content,
        validity=page.validity,
        knowledge_revision_id=page.knowledge_revision_id,
        source_revision_ids=page.source_revision_ids,
        refresh_reasons=page.refresh_reasons,
    )


@pages_router.post("/api/v1/page/delete", response_model=PageDeleteResponse)
async def delete_page_endpoint(
    request: PageDeleteRequest,
    _: None = Depends(require_bearer_token),
) -> PageDeleteResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = (
        await resolve_api_read_scope(kb_dir, request.view_id, request.publication_id)
        if request.dry_run
        else await resolve_api_scope(kb_dir, request.view_id)
    )
    try:
        result = await run_in_threadpool(
            delete_page,
            kb_dir,
            request.path,
            dry_run=request.dry_run,
            scope=scope,
            version=request.version,
            publication_id=request.publication_id,
        )
    except ValueError as exc:  # invalid/traversal-unsafe page ref
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Page not found: {request.path}")
    if result["status"] == "conflict":
        raise HTTPException(status_code=409, detail="Public knowledge or page version changed")
    return PageDeleteResponse(**result)


@pages_router.post("/api/v1/page/links", response_model=PageLinksResponse)
async def page_links_endpoint(
    request: PageLinksRequest,
    _: None = Depends(require_bearer_token),
) -> PageLinksResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = await resolve_api_read_scope(kb_dir, request.view_id, request.publication_id)
    from ultrakb.application.reading import read_page_context

    try:
        context = await run_in_threadpool(read_page_context, kb_dir, request.path, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Page not found: {request.path}")
    return PageLinksResponse(
        status="ok",
        target=context.page.path,
        publication_id=scope.publication_id,
        outlinks=[link.label for link in context.outlinks],
        backlinks=[link.path for link in context.backlinks if link.path is not None],
    )


@pages_router.put("/api/v1/page", response_model=PageEditResponse)
async def edit_page_endpoint(
    request: PageEditRequest,
    _: None = Depends(require_bearer_token),
) -> PageEditResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    scope = await resolve_api_scope(kb_dir, request.view_id)
    try:
        result = await run_in_threadpool(
            save_page,
            kb_dir,
            request.path,
            request.content,
            scope=scope,
            version=request.version,
            publication_id=request.publication_id,
        )
    except ValueError as exc:  # invalid/traversal-unsafe page ref
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.page is None:
        raise HTTPException(status_code=404, detail=f"Page not found: {request.path}")
    if result.status == "conflict":
        raise HTTPException(status_code=409, detail="Public knowledge or page version changed")
    return PageEditResponse(
        publication_id=result.page.publication_id,
        version=result.page.version,
        status=result.status,
        target=result.page.path,
        ghosts_stripped=list(result.ghosts_stripped),
        content=result.page.content,
    )
