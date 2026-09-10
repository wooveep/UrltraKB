"""OCR preparation and cancellation-aware explicit installation endpoints."""

import asyncio
from pathlib import Path
from threading import Event
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import require_bearer_token
from openkb.application.ocr_installation import (
    install_ocr,
    prepare_ocr_install,
    read_ocr_installations,
)

router = APIRouter(dependencies=[Depends(require_bearer_token)])


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Literal["native", "nvidia", "openvino"]
    destination: str | None = None
    offline: str | None = None


@router.get("/api/v1/config/ocr/installations")
async def installations():
    return await run_in_threadpool(read_ocr_installations)


@router.post("/api/v1/config/ocr/prepare")
async def prepare(body: InstallRequest):
    return await run_in_threadpool(
        prepare_ocr_install, body.profile, Path(body.destination) if body.destination else None
    )


@router.post("/api/v1/config/ocr/install")
async def install(body: InstallRequest, request: Request):
    from functools import partial

    operation = partial(
        install_ocr,
        body.profile,
        destination=Path(body.destination) if body.destination else None,
        offline=Path(body.offline) if body.offline else None,
    )
    return await _owned_operation(request, operation)


async def _owned_operation(request, operation):
    stop = Event()
    task = asyncio.create_task(run_in_threadpool(operation, cancelled=stop.is_set))
    try:
        while not task.done():
            if await request.is_disconnected():
                stop.set()
            await asyncio.wait({task}, timeout=0.2)
        return task.result()
    finally:
        stop.set()
        # Thread cancellation is cooperative. It retains process ownership until
        # the installer confirms descendants have exited and records its state.
        if not task.done():
            await asyncio.shield(task)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation: str
    device: Literal["auto", "cpu", "gpu"] = "auto"
    gpu_device: str | None = Field(default=None, pattern=r"^(GPU\.[0-9]+|gpu:[0-9]+)$")


@router.post("/api/v1/config/ocr/check")
async def probe(body: ProbeRequest, request: Request):
    from functools import partial

    from openkb.application.ocr_capability import check_ocr_capability

    return await _owned_operation(
        request,
        partial(check_ocr_capability, body.installation, body.device, gpu_device=body.gpu_device),
    )


class ServiceProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb: str | None = None


@router.post("/api/v1/config/ocr/check-service")
async def probe_service(body: ServiceProbeRequest, request: Request):
    from functools import partial

    from openkb.api_helpers import _resolve_kb
    from openkb.application.ocr_capability import check_ocr_service

    kb = await run_in_threadpool(_resolve_kb, body.kb) if body.kb else None
    return await _owned_operation(request, partial(check_ocr_service, kb))
