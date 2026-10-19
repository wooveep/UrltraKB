"""Serve the exact retained HTML artifact, independent of its editable copy."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ultrakb.api_artifact_reads import artifact_read
from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.application.artifacts import read_artifact
from ultrakb.source_records import RecordId
from ultrakb.storage.wiki_paths import wiki_path

output_router = APIRouter()
_VIEWABLE_SUFFIXES = (".html", ".htm")


@output_router.get("/api/v1/output")
async def output_file_endpoint(
    kb: str = Query(...),
    path: str = Query(..., min_length=1),
    revision_id: RecordId | None = Query(None),
    _: None = Depends(require_bearer_token),
) -> Response:
    kb_dir = await asyncio.to_thread(_resolve_kb, kb)
    try:
        wiki_path(path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid output path.") from error
    rel = PurePosixPath(path)
    if rel.parts[0] != "output":
        raise HTTPException(status_code=400, detail="Path must be under output/.")
    if rel.suffix.lower() not in _VIEWABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only viewable HTML artifacts are served.")
    try:
        content = await artifact_read(read_artifact, kb_dir, path, revision_id=revision_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {path}") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Response(content, media_type="text/html")
