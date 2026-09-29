"""Read and download generated skills and decks."""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_models import DeckListResponse, SkillListResponse

artifacts_router = APIRouter()


@artifacts_router.get("/api/v1/deck", response_model=DeckListResponse)
async def deck_list_endpoint(
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> DeckListResponse:
    from openkb.deck import decks_root

    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    root = decks_root(kb_dir)
    decks = (
        sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.endswith("-workspace"))
        if root.is_dir()
        else []
    )
    return DeckListResponse(decks=[{"name": n} for n in decks])


@artifacts_router.get("/api/v1/deck/{name}")
async def deck_download_endpoint(
    name: str,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> Any:
    from openkb.cli import _validate_skill_name
    from openkb.deck import deck_dir, decks_root

    if _validate_skill_name(name):
        raise HTTPException(status_code=400, detail="Invalid deck name.")
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    root = decks_root(kb_dir).resolve()
    target = deck_dir(kb_dir, name).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid deck name.")
    index = target / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail=f"Deck not found: {name}")
    return FileResponse(index, media_type="text/html")


@artifacts_router.get("/api/v1/skill", response_model=SkillListResponse)
async def skill_list_endpoint(
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> SkillListResponse:
    from openkb.skill import skills_root

    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    root = skills_root(kb_dir)
    skills = (
        sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.endswith("-workspace"))
        if root.is_dir()
        else []
    )
    return SkillListResponse(skills=[{"name": n} for n in skills])


@artifacts_router.get("/api/v1/skill/{name}/archive")
async def skill_archive_endpoint(
    name: str,
    kb: str = Query(...),
    _: None = Depends(require_bearer_token),
) -> Any:
    import io
    import zipfile

    from openkb.cli import _validate_skill_name
    from openkb.skill import skill_dir, skills_root

    if _validate_skill_name(name):
        raise HTTPException(status_code=400, detail="Invalid skill name.")
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    root = skills_root(kb_dir).resolve()
    target = skill_dir(kb_dir, name).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid skill name.")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in target.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(target))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")
