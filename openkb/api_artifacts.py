"""Thin authenticated adapters for the shared artifact use cases."""

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Response

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.application.artifacts import (
    artifact_archive,
    artifact_quality,
    delete_artifact,
    list_artifacts,
)

router = APIRouter(prefix="/api/v1/artifacts", dependencies=[Depends(require_bearer_token)])


async def _call(kb, operation, *args, **kwargs):
    root = await asyncio.to_thread(_resolve_kb, kb)
    try:
        return await asyncio.to_thread(operation, root, *args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Artifact not found") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def listing(kb: str):
    return [asdict(item) for item in await _call(kb, list_artifacts)]


@router.get("/quality")
async def quality(kb: str, path: str):
    return await _call(kb, artifact_quality, path)


@router.get("/export")
async def export(kb: str, path: str, include_evidence: bool = False):
    data = await _call(kb, artifact_archive, path, include_evidence=include_evidence)
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="artifact.zip"',
        },
    )


@router.delete("")
async def delete(kb: str, path: str):
    await _call(kb, delete_artifact, path)
    return {"status": "deleted", "path": path}


async def skill_archive_response(name: str, kb: str, include_evidence: bool):
    """Keep the original Skill download URL and rootless ZIP layout."""
    import io

    from fastapi.responses import StreamingResponse

    from openkb.skill import skill_dir, validate_skill_name

    if validate_skill_name(name):
        raise HTTPException(400, "Invalid skill name.")
    root = await asyncio.to_thread(_resolve_kb, kb)
    if not skill_dir(root, name).is_dir():
        raise HTTPException(404, f"Skill not found: {name}")
    data = await _call(
        kb,
        artifact_archive,
        f"output/skills/{name}",
        include_evidence=include_evidence,
        strip_root=True,
    )
    return StreamingResponse(io.BytesIO(data), media_type="application/zip")


def generation_failure(code, message, result):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=code,
        content={
            "detail": message,
            **{
                key: value
                for key, value in result.items()
                if key not in {"event", "code", "message"}
            },
        },
    )
