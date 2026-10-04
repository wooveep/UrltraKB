"""Read and download generated skills and decks."""

import asyncio
import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_models import DeckListResponse, SkillListResponse
from openkb.application.artifacts import list_artifacts, read_artifact, write_artifact_archive
from openkb.application.generators import validate_name

artifacts_router = APIRouter()


@artifacts_router.get("/api/v1/deck", response_model=DeckListResponse)
async def deck_list_endpoint(
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> DeckListResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    artifacts = await asyncio.to_thread(list_artifacts, kb_dir)
    decks = [Path(item.path).name for item in artifacts if item.kind == "幻灯片"]
    return DeckListResponse(decks=[{"name": n} for n in decks])


@artifacts_router.get("/api/v1/deck/{name}")
async def deck_download_endpoint(
    name: str,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> Any:
    if validate_name(name):
        raise HTTPException(status_code=400, detail="Invalid deck name.")
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    try:
        content = await asyncio.to_thread(read_artifact, kb_dir, f"output/decks/{name}/index.html")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Deck not found: {name}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content, media_type="text/html")


@artifacts_router.get("/api/v1/skill", response_model=SkillListResponse)
async def skill_list_endpoint(
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> SkillListResponse:
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    artifacts = await asyncio.to_thread(list_artifacts, kb_dir)
    skills = [Path(item.path).name for item in artifacts if item.kind == "Skill"]
    return SkillListResponse(skills=[{"name": n} for n in skills])


@artifacts_router.get("/api/v1/skill/{name}/archive")
async def skill_archive_endpoint(
    name: str,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> Any:
    if validate_name(name):
        raise HTTPException(status_code=400, detail="Invalid skill name.")
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    buf = io.BytesIO()
    try:
        await asyncio.to_thread(write_artifact_archive, kb_dir, f"output/skills/{name}", buf)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(buf.getvalue(), media_type="application/zip")
