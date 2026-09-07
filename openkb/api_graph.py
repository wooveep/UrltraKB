"""Knowledge-graph REST endpoints.

Split into an APIRouter (the first in this codebase) so ``api.py`` stays under
the per-file line limit (``tests/test_file_size.py``). Both endpoints are
read-only and depend only on module-level helpers (``_resolve_kb``,
``require_bearer_token``) — no ``create_app`` closures — so they extract cleanly.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_models import GraphRequest, GraphResponse

graph_router = APIRouter()


@graph_router.post("/api/v1/graph", response_model=GraphResponse)
async def graph_endpoint(
    request: GraphRequest,
    _: None = Depends(require_bearer_token),
) -> GraphResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, request.kb)
    from openkb.application.artifacts import read_graph

    graph = await run_in_threadpool(read_graph, kb_dir)
    return GraphResponse(**graph)


@graph_router.get("/api/v1/graph/html")
async def graph_html_endpoint(
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> HTMLResponse:
    # Self-contained graph HTML (the same renderer the ``openkb visualize`` CLI
    # writes to disk) for the Workbench's sandboxed iframe / new tab. The POST
    # JSON variant above feeds the in-chat card's node/edge counts.
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    from openkb.application.artifacts import graph_html

    html = await run_in_threadpool(graph_html, kb_dir)
    return HTMLResponse(content=html)
