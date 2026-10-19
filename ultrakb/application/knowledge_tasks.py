"""Shared authoritative task pages and the identical bounded JSON export schema."""

from pathlib import Path

from ultrakb.export_paths import require_external_export
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.locks import atomic_write_text
from ultrakb.storage.database import open_database
from ultrakb.storage.task_store import TaskStore
from ultrakb.task_records import TaskDetail, TaskList


def capture_task_identity(kb_dir: Path):
    """Bind a task browser before asynchronous reads, including an unpublished KB."""
    with open_database(kb_dir, read_only=True) as db:
        return db.identity


def _store(kb_dir, scope, identity=None):
    if scope is None:
        with open_database(kb_dir, expected=identity, read_only=True) as db:
            return TaskStore(db.root, db.identity)
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("Task queries require a main-store knowledge base")
    if identity is not None and scope.knowledge.identity != identity:
        raise ValueError("Task query belongs to another knowledge-base generation")
    return TaskStore(scope.kb_dir, scope.knowledge.identity)


def list_knowledge_tasks(
    kb_dir: Path, *, scope=None, identity=None, after=0, limit=63, cancelled=None
) -> TaskList:
    return _store(kb_dir, scope, identity).list(after=after, limit=limit, cancelled=cancelled)


def read_knowledge_task(
    kb_dir: Path,
    task_id: str,
    *,
    scope=None,
    identity=None,
    execution_id=None,
    limit=63,
    executions_after=0,
    stages_after=0,
    requests_after=0,
    publications_after=0,
    cancelled=None,
) -> TaskDetail:
    return _store(kb_dir, scope, identity).read(
        task_id,
        execution_id=execution_id,
        limit=limit,
        executions_after=executions_after,
        stages_after=stages_after,
        requests_after=requests_after,
        publications_after=publications_after,
        cancelled=cancelled,
    )


def read_knowledge_execution(kb_dir: Path, execution_id: str, **options) -> TaskDetail:
    """Resolve a worker execution to its authoritative task, without guessing app IDs."""
    store = _store(kb_dir, options.pop("scope", None), options.pop("identity", None))
    return store.for_execution(execution_id, **options)


def export_knowledge_task(kb_dir: Path, task_id: str, destination: Path, **options) -> TaskDetail:
    destination = require_external_export(kb_dir, destination.expanduser())
    result = read_knowledge_task(kb_dir, task_id, **options)
    atomic_write_text(destination, result.model_dump_json(indent=2))
    return result
