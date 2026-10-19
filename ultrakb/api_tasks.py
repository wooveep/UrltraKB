"""Authenticated task query adapters; all storage work runs off the event loop."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ultrakb.api_helpers import _resolve_kb, require_bearer_token
from ultrakb.application.knowledge_tasks import list_knowledge_tasks, read_knowledge_task
from ultrakb.task_records import TaskDetail, TaskList

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
    def read():
        return operation(_resolve_kb(kb), *args, **options)

    try:
        return await run_in_threadpool(read)
    except KeyError as exc:
        raise HTTPException(404, "Task or execution was not found") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@tasks_router.get("", response_model=TaskList)
async def list_tasks(
    kb: str,
    after: int = Query(0, ge=0, lt=2**63),
    limit: int = Query(63, ge=1, le=128),
):
    return await _query(list_knowledge_tasks, kb, after=after, limit=limit)


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
