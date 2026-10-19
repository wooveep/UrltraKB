"""Attach actual SDK calls to native executions and query their durable receipts."""

from contextlib import contextmanager

from ultrakb.llm_usage import UsageScope
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import open_database
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.model_usage import StoredModelUsage
from ultrakb.storage.usage_store import UsageStore


def observe_executor(root, executor, *, work=None):
    with open_database(root, read_only=True) as db:
        identity = db.identity
    executions = ExecutionStore(root, identity)
    execution = (
        executions.execution(work.execution_id)
        if work is not None
        else executions.begin(
            "sdk-protocol-test",
            fingerprint=executor.fingerprint,
            max_calls=executor.policy.max_calls,
        )
    )
    observer = StoredModelUsage(root, identity, execution)
    executor.scope = UsageScope(root, execution.execution_id, storage=observer)
    return executor


def _store_executions(root):
    with open_database(root, read_only=True) as db:
        identity = db.identity
    with storage_stage(root, identity, read_only=True) as db:
        executions = [
            row[0]
            for row in db.connection.execute(
                "SELECT execution_id FROM ukb_executions ORDER BY sequence"
            ).fetchall()
        ]
    return UsageStore(root, identity), executions


def read_requests(root):
    store, executions = _store_executions(root)
    result = []
    for execution in executions:
        cursor = 0
        while True:
            page = store.requests(execution_id=execution, after=cursor, limit=128)
            result.extend(page.records)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    return result


def aggregate_usage(root, **selection):
    store, executions = _store_executions(root)
    return (
        store.summary(**selection)
        if selection
        else store.selected_summary([("execution", set(executions))])
    )


@contextmanager
def native_usage_execution(root, operation, *, task_id=None, on_event=None):
    """Real native receipt ownership for isolated SDK/transport protocol tests."""
    from ultrakb.llm_usage import _SCOPE

    with open_database(root, read_only=True) as db:
        identity = db.identity
    executions = ExecutionStore(root, identity)
    execution = executions.begin(
        operation,
        fingerprint="explicit-transport-fixture",
        max_calls=None,
        application_task_id=task_id,
    )
    observer = StoredModelUsage(root, identity, execution, on_event=on_event)
    scope = UsageScope(root, execution.execution_id, storage=observer, on_event=on_event)
    token = _SCOPE.set(scope)
    state = "completed"
    try:
        yield scope
    except BaseException as error:
        from ultrakb.llm_usage_transport import _failure_state

        state = _failure_state(error)
        raise
    finally:
        try:
            observer.close()
        finally:
            executions.finish(execution.execution_id, state=state, work_settled=True)
            _SCOPE.reset(token)
