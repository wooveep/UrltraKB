"""Durable actual non-model boundaries, independent of UI progress subscription."""

import logging
from contextlib import contextmanager
from pathlib import Path

from ultrakb.locks import LockCancelled
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.stage_context import bind_stage, parent_stage

logger = logging.getLogger(__name__)


@contextmanager
def task_stage(context, code, *, ensure_work=False, allow_stopping=False, **identity):
    execution = context.storage_execution
    if execution is None:
        raise RuntimeError("A durable stage requires its top-level execution first")
    root = Path(context.snapshot.kb_dir)
    store = ExecutionStore(root, context.storage_identity)
    try:
        stage_id = store.start_stage(
            execution.execution_id,
            code,
            allow_stopping=allow_stopping,
            parent_stage_id=parent_stage(root, context.storage_identity, execution.execution_id),
            **identity,
        )
    except Exception:
        if not ensure_work:
            raise
        # Missing observations must never skip actual SDK/worker cleanup.
        yield {}
        raise
    failure = None
    try:
        with (
            bind_stage(root, context.storage_identity, execution.execution_id, stage_id),
            context.activity(code) as progress,
        ):
            progress["stage_attempt_id"] = stage_id
            progress["execution_id"] = execution.execution_id
            yield progress
    except BaseException as error:
        failure = error
        raise
    finally:
        state = (
            "completed"
            if failure is None
            else "cancelled"
            if isinstance(failure, (LockCancelled, GeneratorExit))
            or type(failure).__name__ == "CancelledError"
            else "failed"
        )
        try:
            store.finish_stage(
                stage_id, state=state, error_code=type(failure).__name__ if failure else None
            )
        except Exception:
            if failure is None:
                raise
            logger.warning("Could not confirm stage termination", exc_info=True)
