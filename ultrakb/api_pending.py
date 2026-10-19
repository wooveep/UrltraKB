"""Pending work remains independent from the document body's outcome."""

import asyncio
from threading import Event

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ConfigDict, Field

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.api_views import ViewRequest, resolve_api_scope
from ultrakb.application import pending
from ultrakb.application.execution import ExecutionContext
from ultrakb.config import resolve_kb_alias
from ultrakb.context.workers import run_blocking
from ultrakb.source_records import RecordId, ViewId

pending_router = APIRouter(dependencies=[Depends(require_bearer_token)])


class PendingRequest(ViewRequest):
    model_config = ConfigDict(extra="forbid")
    kb: str


class DrainRequest(PendingRequest):
    max_jobs: int = Field(default=10000, ge=1, le=10000)


class JobRequest(PendingRequest):
    job_id: RecordId


class GroupRequest(PendingRequest):
    group_id: RecordId


class BudgetRequest(GroupRequest):
    limits: dict[str, int | float]


async def invoke(function, kb, *args, view_id=None, on_cancel=None, **kwargs):
    try:
        root = await asyncio.to_thread(resolve_kb_alias if view_id is not None else _resolve_kb, kb)
        scope = await resolve_api_scope(root, view_id)
        return await run_blocking(
            lambda: function(root, *args, scope=scope, **kwargs), on_cancel=on_cancel
        )
    except KeyError as exc:
        raise HTTPException(404, "Unknown pending job") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@pending_router.get("/api/v1/pending")
async def inventory(
    kb: str = "default",
    view_id: ViewId | None = None,
    after: int = Query(0, ge=0, lt=2**63),
    limit: int = Query(63, ge=1, le=128),
):
    return await invoke(pending.pending_status, kb, view_id=view_id, after=after, limit=limit)


@pending_router.get("/api/v1/pending/{job_id}")
async def detail(
    job_id: RecordId,
    kb: str = "default",
    view_id: ViewId | None = None,
    after_object: int = Query(0, ge=0, lt=2**63),
    limit: int = Query(63, ge=1, le=128),
):
    return await invoke(
        pending.read_pending_job,
        kb,
        job_id,
        view_id=view_id,
        checkpoint_after=after_object,
        limit=limit,
    )


@pending_router.post("/api/v1/process-pending")
async def drain(request: DrainRequest):
    stopped = Event()
    return await invoke(
        pending.process_pending,
        request.kb,
        view_id=request.view_id,
        max_jobs=request.max_jobs,
        context=ExecutionContext(cancelled=stopped.is_set),
        on_cancel=stopped.set,
    )


@pending_router.post("/api/v1/pending/retry")
async def retry(request: JobRequest):
    await invoke(pending.retry_pending_job, request.kb, request.job_id, view_id=request.view_id)
    return {"status": "queued"}


@pending_router.post("/api/v1/pending/cancel-group")
async def cancel(request: GroupRequest):
    await invoke(
        pending.cancel_execution_group, request.kb, request.group_id, view_id=request.view_id
    )
    return {"status": "cancelled"}


@pending_router.post("/api/v1/pending/budget")
async def budget(request: BudgetRequest):
    return await invoke(
        pending.update_execution_budget,
        request.kb,
        request.group_id,
        request.limits,
        view_id=request.view_id,
    )
