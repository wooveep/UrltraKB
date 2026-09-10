"""Source state and review; continuing work belongs to the shared task owner."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_tasks import task_payload
from openkb.application.source_actions import (
    inspect_source_parse,
    read_source_evidence,
    review_source_proposal,
)
from openkb.application.source_history import source_status
from openkb.evidence import Evidence
from openkb.runtime.requests import (
    CleanupSourceHistory,
    ConfirmSourcePage,
    ContinueSource,
    RebuildSourceNavigation,
    ReparseSource,
    ReprocessSourcePage,
)
from openkb.sources import SourceStore, content_id

SourceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
VersionId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
router = APIRouter(dependencies=[Depends(require_bearer_token)])


class SourceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb: str
    source_id: SourceId


class CleanupPreviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb: str


class CleanupQuery(CleanupPreviewQuery):
    preview_id: VersionId
    task_id: SourceId | None = None


@router.post("/api/v1/source/cleanup-preview")
async def cleanup_preview(query: CleanupPreviewQuery):
    from dataclasses import asdict

    from openkb.application.source_cleanup import preview_history_cleanup

    root = await asyncio.to_thread(_resolve_kb, query.kb)
    return asdict(await asyncio.to_thread(preview_history_cleanup, root))


@router.post("/api/v1/source/cleanup", status_code=202)
async def cleanup(query: CleanupQuery, request: Request):
    return await _submit(query, request, CleanupSourceHistory(query.preview_id))


class ProposalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb: str
    proposal_id: VersionId
    page: str | None = None
    max_chars: int = Field(default=100_000, ge=1, le=1_000_000)


class SourceContinuation(SourceQuery):
    version_id: VersionId
    proposal_id: VersionId | None = None
    accept_pages: list[str] | None = None
    task_id: SourceId | None = None


class SourceVersionQuery(SourceQuery):
    version_id: VersionId


class SourceMutation(SourceVersionQuery):
    task_id: SourceId | None = None


class PageDecision(SourceMutation):
    parse_id: VersionId
    page: int = Field(ge=1)
    reason: Literal["legitimate_blank", "legitimate_illustration"]


class NavigationRebuild(SourceMutation):
    parse_id: VersionId


class NavigationQuery(SourceVersionQuery):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


@router.post("/api/v1/source/rebuild-navigation", status_code=202)
async def rebuild_navigation(query: NavigationRebuild, request: Request):
    return await _submit(
        query, request, RebuildSourceNavigation(query.source_id, query.version_id, query.parse_id)
    )


@router.post("/api/v1/source/navigation")
async def navigation(query: NavigationQuery):
    from openkb.navigation import read_navigation

    root = await asyncio.to_thread(_resolve_kb, query.kb)

    def read():
        source = SourceStore(root).version(query.version_id)
        if source.source_id != query.source_id:
            raise ValueError("Source identity mismatch")
        return read_navigation(root, source, offset=query.offset, limit=query.limit)

    try:
        return await asyncio.to_thread(read)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Source navigation is unavailable") from exc


class PageReprocessing(SourceMutation):
    parse_id: VersionId
    page: int = Field(ge=1)
    acknowledge_unknown: bool = Field(default=False, strict=True)
    engine: Literal["system", "local", "cloud"] | None = None


async def _submit(query, request, unit):
    root = await asyncio.to_thread(_resolve_kb, query.kb)
    manager = request.app.state.import_tasks.manager
    binding = content_id(
        {"operation": type(unit).__name__, **query.model_dump(exclude={"kb", "task_id"})}
    )
    try:
        # Once admission starts, disconnect only ends observation of this task.
        task_id = await asyncio.shield(
            asyncio.to_thread(
                manager.submit, root, [unit], task_id=query.task_id, input_binding=binding
            )
        )
        return task_payload(manager.get(task_id))
    except ValueError as exc:
        raise HTTPException(409, "Source operation conflicts with the requested task") from exc


@router.post("/api/v1/source/confirm-page", status_code=202)
async def confirm_page(query: PageDecision, request: Request):
    unit = ConfirmSourcePage(**query.model_dump(exclude={"kb", "task_id"}))
    return await _submit(query, request, unit)


@router.post("/api/v1/source/reprocess-page", status_code=202)
async def reprocess_page(query: PageReprocessing, request: Request):
    unit = ReprocessSourcePage(**query.model_dump(exclude={"kb", "task_id"}))
    return await _submit(query, request, unit)


@router.post("/api/v1/source/reparse", status_code=202)
async def reparse(query: SourceMutation, request: Request):
    return await _submit(query, request, ReparseSource(query.source_id, query.version_id))


class ParseQuery(SourceVersionQuery):
    parse_id: VersionId
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class EvidenceQuery(SourceVersionQuery):
    parse_id: VersionId
    block_id: VersionId
    start: int = Field(default=0, ge=0)
    end: int | None = Field(default=None, ge=1)
    max_chars: int = Field(default=16_000, ge=1, le=1_000_000)


@router.post("/api/v1/source/original")
async def original(query: SourceVersionQuery):
    root = await asyncio.to_thread(_resolve_kb, query.kb)

    def read():
        store = SourceStore(root)
        version = store.version(query.version_id)
        if version.source_id != query.source_id:
            raise ValueError("Source identity mismatch")
        return store.original(version), version.name

    try:
        path, name = await asyncio.to_thread(read)
        return FileResponse(path, filename=name)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Source original is unavailable") from exc


@router.post("/api/v1/source/parse")
async def parse(query: ParseQuery):
    root = await asyncio.to_thread(_resolve_kb, query.kb)
    try:
        return await asyncio.to_thread(
            inspect_source_parse, root, **query.model_dump(exclude={"kb"})
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Source parse is unavailable") from exc


@router.post("/api/v1/source/evidence")
async def evidence(query: EvidenceQuery):
    from dataclasses import asdict

    root = await asyncio.to_thread(_resolve_kb, query.kb)
    try:
        reference = Evidence(**query.model_dump(exclude={"kb", "max_chars"}))
        result = await asyncio.to_thread(
            read_source_evidence, root, reference, max_chars=query.max_chars
        )
        return asdict(result)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Source evidence is unavailable") from exc


@router.post("/api/v1/source/status")
async def status(query: SourceQuery):
    root = await asyncio.to_thread(_resolve_kb, query.kb)
    try:
        return await asyncio.to_thread(source_status, root, query.source_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Source is unavailable") from exc


@router.post("/api/v1/source/proposal")
async def proposal(query: ProposalQuery):
    root = await asyncio.to_thread(_resolve_kb, query.kb)
    try:
        return await asyncio.to_thread(
            review_source_proposal,
            root,
            query.proposal_id,
            page=query.page,
            max_chars=query.max_chars,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(409, "Proposal is unavailable or exceeds the review limit") from exc


@router.post("/api/v1/source/continue", status_code=202)
async def continue_saved_source(query: SourceContinuation, request: Request):
    try:
        unit = ContinueSource(
            query.source_id,
            query.version_id,
            query.proposal_id,
            tuple(query.accept_pages) if query.accept_pages is not None else None,
        )
        return await _submit(query, request, unit)
    except ValueError as exc:
        raise HTTPException(409, "Source continuation conflicts with the requested task") from exc
