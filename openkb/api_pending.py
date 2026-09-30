"""Pending work remains independent from the document body's outcome."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.application import pending
from openkb.source_records import RecordId

pending_router = APIRouter(dependencies=[Depends(require_bearer_token)])


class PendingRequest(BaseModel):
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


async def invoke(function, kb, *args, **kwargs):
    root = await asyncio.to_thread(_resolve_kb, kb)
    try:
        return await asyncio.to_thread(function, root, *args, **kwargs)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@pending_router.get("/api/v1/pending")
async def inventory(kb: str = "default"):
    return await invoke(pending.pending_status, kb)


@pending_router.post("/api/v1/process-pending")
async def drain(request: DrainRequest):
    return await invoke(pending.process_pending, request.kb, max_jobs=request.max_jobs)


@pending_router.post("/api/v1/pending/retry")
async def retry(request: JobRequest):
    await invoke(pending.retry_pending_job, request.kb, request.job_id)
    return {"status": "queued"}


@pending_router.post("/api/v1/pending/cancel-group")
async def cancel(request: GroupRequest):
    await invoke(pending.cancel_execution_group, request.kb, request.group_id)
    return {"status": "cancelled"}


@pending_router.post("/api/v1/pending/budget")
async def budget(request: BudgetRequest):
    return await invoke(
        pending.update_execution_budget, request.kb, request.group_id, request.limits
    )
