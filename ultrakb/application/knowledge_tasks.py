"""Shared authoritative task pages and the identical bounded JSON export schema."""

from pathlib import Path

from ultrakb.knowledge_scope import resolve_scope
from ultrakb.locks import atomic_write_text
from ultrakb.storage.task_store import TaskStore
from ultrakb.task_records import TaskDetail, TaskList


def _store(kb_dir, scope):
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("Task queries require a main-store knowledge base")
    return TaskStore(scope.kb_dir, scope.knowledge.identity)


def list_knowledge_tasks(kb_dir: Path, *, scope=None, after=0, limit=63) -> TaskList:
    return _store(kb_dir, scope).list(after=after, limit=limit)


def read_knowledge_task(
    kb_dir: Path,
    task_id: str,
    *,
    scope=None,
    execution_id=None,
    limit=63,
    executions_after=0,
    stages_after=0,
    requests_after=0,
) -> TaskDetail:
    return _store(kb_dir, scope).read(
        task_id,
        execution_id=execution_id,
        limit=limit,
        executions_after=executions_after,
        stages_after=stages_after,
        requests_after=requests_after,
    )


def export_knowledge_task(kb_dir: Path, task_id: str, destination: Path, **options) -> TaskDetail:
    destination = destination.expanduser().absolute()
    if destination.resolve().is_relative_to(kb_dir.resolve()):
        raise ValueError(
            "Task diagnostics must be exported outside the authoritative knowledge base"
        )
    result = read_knowledge_task(kb_dir, task_id, **options)
    atomic_write_text(destination, result.model_dump_json(indent=2))
    return result
