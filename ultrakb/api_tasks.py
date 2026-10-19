"""Authenticated task query adapters; all storage work runs off the event loop."""

from threading import Event

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ultrakb.api_helpers import require_bearer_token
from ultrakb.application.knowledge_tasks import (
    list_knowledge_tasks,
    read_knowledge_stage,
    read_knowledge_task,
    set_knowledge_task_visibility,
)
from ultrakb.config import resolve_kb_alias
from ultrakb.context.workers import run_blocking
from ultrakb.source_records import RecordId
from ultrakb.storage.database import DatabaseIdentity, StorageFormatError
from ultrakb.task_records import TaskDetail, TaskList, TaskOverview, TaskRecord, TaskStageDetail

tasks_router = APIRouter(prefix="/api/v1/tasks", dependencies=[Depends(require_bearer_token)])


def detail_options(
    execution_id: str | None = None,
    limit: int = Query(63, ge=1, le=128),
    executions_after: int = Query(0, ge=0, lt=2**63),
    stages_after: int = Query(0, ge=0, lt=2**63),
    requests_after: int = Query(0, ge=0, lt=2**63),
    publications_after: int = Query(0, ge=0, lt=2**63),
):
    return dict(
        execution_id=execution_id,
        limit=limit,
        executions_after=executions_after,
        stages_after=stages_after,
        requests_after=requests_after,
        publications_after=publications_after,
    )


async def _query(operation, kb, *args, **options):
    cancelled = Event()

    def read():
        return operation(resolve_kb_alias(kb), *args, cancelled=cancelled.is_set, **options)

    try:
        return await run_blocking(read, on_cancel=cancelled.set)
    except KeyError as exc:
        raise HTTPException(404, "Task or execution was not found") from exc
    except StorageFormatError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@tasks_router.get("", response_model=TaskList)
async def list_tasks(
    kb: str,
    after: int = Query(0, ge=0, lt=2**63),
    limit: int = Query(63, ge=1, le=128),
    include_hidden: bool = False,
):
    return await _query(
        list_knowledge_tasks, kb, after=after, limit=limit, include_hidden=include_hidden
    )


class TaskVisibilityRequest(TaskRecord):
    kb_id: RecordId
    generation: RecordId
    hidden: bool


@tasks_router.post("/{task_id}/visibility", response_model=TaskOverview)
async def set_visibility(task_id: str, kb: str, request: TaskVisibilityRequest):
    return await _query(
        set_knowledge_task_visibility,
        kb,
        task_id,
        hidden=request.hidden,
        identity=DatabaseIdentity(request.kb_id, request.generation),
    )


@tasks_router.get("/{task_id}", response_model=TaskDetail)
async def read_task(task_id: str, kb: str, options: dict = Depends(detail_options)):
    return await _query(read_knowledge_task, kb, task_id, **options)


@tasks_router.get("/{task_id}/export")
async def export_task(task_id: str, kb: str, options: dict = Depends(detail_options)):
    result = await _query(read_knowledge_task, kb, task_id, **options)
    return Response(
        result.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="task-{result.task.task_id}.json"'},
    )


def stage_options(
    limit: int = Query(63, ge=1, le=128),
    requests_after: int = Query(0, ge=0, lt=2**63),
):
    return dict(limit=limit, requests_after=requests_after)


@tasks_router.get("/{task_id}/stages/{stage_id}", response_model=TaskStageDetail)
async def read_stage(task_id: str, stage_id: str, kb: str, options: dict = Depends(stage_options)):
    return await _query(read_knowledge_stage, kb, task_id, stage_id, **options)


@tasks_router.get("/{task_id}/stages/{stage_id}/export")
async def export_stage(
    task_id: str, stage_id: str, kb: str, options: dict = Depends(stage_options)
):
    result = await _query(read_knowledge_stage, kb, task_id, stage_id, **options)
    return Response(
        result.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="stage-{stage_id}.json"'},
    )
