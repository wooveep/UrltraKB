"""Authenticated storage maintenance with explicit generation preconditions."""

from dataclasses import asdict
from pathlib import Path
from threading import Event

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ultrakb.api_helpers import require_bearer_token
from ultrakb.application.storage_maintenance import (
    archive_compiler_diagnostics,
    archive_task_observations,
    audit_knowledge_usage,
    backup_knowledge,
    collect_garbage,
    preview_storage_maintenance,
    repair_knowledge_receipts,
    repair_knowledge_usage,
    restore_knowledge,
    retire_unused_roots,
)
from ultrakb.config import resolve_kb_alias
from ultrakb.context.workers import run_blocking
from ultrakb.storage.database import DatabaseIdentity, StorageFormatError

storage_router = APIRouter(prefix="/api/v1/storage", dependencies=[Depends(require_bearer_token)])


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kb: str = Field(min_length=1)
    kb_id: str = Field(min_length=1, max_length=256)
    generation: str = Field(min_length=1, max_length=256)
    max_items: int = Field(default=128, ge=1, le=1000)
    max_bytes: int = Field(default=64 * 1024**2, ge=1)


class BackupRequest(MaintenanceRequest):
    destination: str = Field(min_length=1)


class RootRetirementRequest(MaintenanceRequest):
    max_bytes: int = Field(default=256 * 1024, ge=1)


class BoundedMaintenanceRequest(RootRetirementRequest):
    max_items: int = Field(default=128, ge=1, le=128)
    max_bytes: int = Field(default=256 * 1024, ge=1, le=256 * 1024)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    backup: str = Field(min_length=1)
    destination: str = Field(min_length=1)


async def _call(operation, *args, on_cancel=None, **options):
    try:
        return asdict(await run_blocking(lambda: operation(*args, **options), on_cancel=on_cancel))
    except StorageFormatError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@storage_router.get("/preview")
async def preview(
    kb: str, max_items: int = Query(128, ge=1, le=1000), max_bytes: int = Query(64 * 1024**2, ge=1)
):
    return await _cancellable(
        preview_storage_maintenance, kb, max_items=max_items, max_bytes=max_bytes
    )


async def _maintain(operation, request):
    return await _call(
        lambda: operation(
            resolve_kb_alias(request.kb),
            identity=DatabaseIdentity(request.kb_id, request.generation),
            max_items=request.max_items,
            max_bytes=request.max_bytes,
        )
    )


@storage_router.post("/retire-roots")
async def retire(request: RootRetirementRequest):
    return await _maintain(retire_unused_roots, request)


@storage_router.post("/collect")
async def collect(request: MaintenanceRequest):
    return await _maintain(collect_garbage, request)


@storage_router.post("/repair-receipts")
async def repair(request: MaintenanceRequest):
    return await _maintain(repair_knowledge_receipts, request)


async def _cancellable(operation, kb, **options):
    cancelled = Event()
    return await _call(
        lambda: operation(resolve_kb_alias(kb), cancelled=cancelled.is_set, **options),
        on_cancel=cancelled.set,
    )


@storage_router.get("/usage-audit")
async def usage_audit(
    kb: str,
    max_items: int = Query(128, ge=1, le=128),
    max_bytes: int = Query(256 * 1024, ge=1, le=256 * 1024),
):
    return await _cancellable(audit_knowledge_usage, kb, max_items=max_items, max_bytes=max_bytes)


@storage_router.post("/repair-usage")
async def repair_usage(request: BoundedMaintenanceRequest):
    return await _cancellable(
        repair_knowledge_usage,
        request.kb,
        identity=DatabaseIdentity(request.kb_id, request.generation),
        max_items=request.max_items,
        max_bytes=request.max_bytes,
    )


@storage_router.post("/archive-observations")
async def archive_observations(request: BoundedMaintenanceRequest):
    return await _cancellable(
        archive_task_observations,
        request.kb,
        identity=DatabaseIdentity(request.kb_id, request.generation),
        max_items=request.max_items,
        max_bytes=request.max_bytes,
    )


@storage_router.post("/backup")
async def backup(request: BackupRequest):
    return await _call(
        lambda: backup_knowledge(
            resolve_kb_alias(request.kb),
            Path(request.destination).expanduser(),
            identity=DatabaseIdentity(request.kb_id, request.generation),
        )
    )


@storage_router.post("/archive-diagnostics")
async def archive_diagnostics(request: MaintenanceRequest):
    return await _cancellable(
        archive_compiler_diagnostics,
        request.kb,
        identity=DatabaseIdentity(request.kb_id, request.generation),
        max_items=request.max_items,
        max_bytes=request.max_bytes,
    )


@storage_router.post("/restore")
async def restore(request: RestoreRequest):
    return await _call(
        restore_knowledge, Path(request.backup).expanduser(), Path(request.destination).expanduser()
    )
