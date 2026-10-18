"""Joint-development model execution without a whole-operation storage lease."""

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ultrakb.config import LlmCredentialBundle
from ultrakb.lifecycle import read_lifecycle
from ultrakb.llm_execution import executor_from_snapshot
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.model_usage import StoredModelUsage

from .execution import ExecutionContext, _activate_context, current_execution
from .execution_configuration import capture_storage_config

logger = logging.getLogger(__name__)


@contextmanager
def begin_storage_execution(root: Path, context: ExecutionContext) -> Iterator[LlmCredentialBundle]:
    root = root.resolve()
    identity = context.storage_identity
    assert identity is not None
    with read_lifecycle(
        root, database_identity=identity, cancelled=context.cancelled, on_wait=context.waiting
    ):
        if current_execution() is context:
            context.check_stop()
            assert context.snapshot is not None
            yield LlmCredentialBundle(**context.snapshot.values()["credentials"])
            return
        with context._event_guard:
            if context._storage_running:
                raise RuntimeError("An execution context cannot run in two operations at once")
            context._storage_running = True
        try:
            if context.resume_model_task_id is not None or context.model_task is not None:
                raise ValueError("A new-store execution cannot resume a legacy model-task ledger")
            if context.snapshot is None:
                with context.activity("preparing"):
                    context.snapshot = capture_storage_config(
                        root, identity, cancelled=context.cancelled, on_wait=context.waiting
                    )
                context.on_snapshot(context.snapshot)
            if context.snapshot.kb_dir != str(root):
                raise ValueError("Execution context belongs to another knowledge base")
            if context.require_verified_connection:
                from .model_connections import require_verified_connection

                require_verified_connection(context.snapshot)
            if context.install_process_settings:
                context.snapshot.install_worker_environment()
            if context.executor is None:
                context.executor = executor_from_snapshot(context.snapshot, context.cancelled)
            executor = context.executor
            store = ExecutionStore(root, identity)
            previous = context.storage_execution
            deadline = (
                store.budget(previous.model_budget_id).deadline_at
                if previous
                else time.time() + executor.policy.deadline - time.monotonic()
                if executor.policy.deadline is not None
                else None
            )
            execution = store.begin(
                "model_task",
                fingerprint=executor.fingerprint,
                max_calls=executor.policy.max_calls,
                deadline_at=deadline,
                resume=previous.execution_id if previous else None,
            )
            context.storage_execution = execution
            usage = StoredModelUsage(root, identity, execution, on_event=context.on_event)
            state = "completed"
            failure = None
            try:
                with (
                    context.snapshot.activate(),
                    executor.activate(),
                    _activate_context(context),
                    usage.activate(),
                ):
                    context.check_stop()
                    yield LlmCredentialBundle(**context.snapshot.values()["credentials"])
                    if usage.failure is not None:
                        raise usage.failure
            except BaseException as error:
                from ultrakb.llm_usage_transport import _failure_state

                state = _failure_state(error)
                failure = error
                raise
            finally:
                try:
                    try:
                        usage.close()
                    finally:
                        store.finish(
                            execution.execution_id,
                            state="failed" if state == "completed" and usage.failure else state,
                            work_settled=True,
                        )
                except Exception:
                    if failure is None:
                        raise
                    logger.warning("Could not confirm execution cleanup", exc_info=True)
        finally:
            with context._event_guard:
                context._storage_running = False
