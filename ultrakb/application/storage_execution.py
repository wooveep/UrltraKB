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
def _running(context):
    with context._event_guard:
        if context._storage_running:
            raise RuntimeError("An execution context cannot run in two operations at once")
        context._storage_running = True
    try:
        yield
    finally:
        with context._event_guard:
            context._storage_running = False


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
        with _running(context):
            if context.resume_model_task_id is not None or context.model_task is not None:
                raise ValueError("A new-store execution cannot resume a legacy model-task ledger")
            if context.snapshot is None:
                with context.activity("preparing"):
                    context.snapshot = capture_storage_config(
                        root,
                        identity,
                        bundle=context.credential_bundle,
                        cancelled=context.cancelled,
                        on_wait=context.waiting,
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
            if previous is None and context.resume_storage_execution_id is not None:
                previous = store.execution(context.resume_storage_execution_id)
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
                pending_attempt_id=context.storage_pending_attempt_id,
            )
            context.storage_execution = execution
            context._storage_result = "completed"
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
                        from .task_stages import task_stage

                        with task_stage(
                            context, "execution.cleanup.v1", ensure_work=True, allow_stopping=True
                        ):
                            usage.close()
                    finally:
                        store.finish(
                            execution.execution_id,
                            state=("failed" if usage.failure else context._storage_result)
                            if state == "completed"
                            else state,
                            work_settled=True,
                        )
                except Exception:
                    if failure is None:
                        raise
                    logger.warning("Could not confirm execution cleanup", exc_info=True)


@contextmanager
def begin_storage_operation(root: Path, context: ExecutionContext, operation: str):
    """Persist a non-model operation without requiring model settings or credentials."""
    import hashlib

    root = root.resolve()
    identity = context.storage_identity
    if identity is None:
        raise ValueError("A storage operation requires an explicit database identity")
    with read_lifecycle(
        root, database_identity=identity, cancelled=context.cancelled, on_wait=context.waiting
    ):
        if current_execution() is context:
            context.check_stop()
            yield
            return
        with _running(context):
            context.check_stop()
            store = ExecutionStore(root, identity)
            previous = context.storage_execution
            resume = previous.execution_id if previous else context.resume_storage_execution_id
            context.storage_execution = store.begin(
                operation,
                fingerprint=hashlib.sha256(
                    ("storage-operation-v1:" + operation).encode()
                ).hexdigest(),
                max_calls=0,
                resume=resume,
                pending_attempt_id=context.storage_pending_attempt_id,
            )
            context._storage_result = "failed"
            state, failure = "completed", None
            try:
                with _activate_context(context):
                    yield
            except BaseException as error:
                from ultrakb.llm_usage_transport import _failure_state

                state, failure = _failure_state(error), error
                raise
            finally:
                try:
                    store.finish(
                        context.storage_execution.execution_id,
                        state="completed"
                        if context._storage_result == "completed"
                        else context._storage_result
                        if state == "completed"
                        else state,
                        work_settled=True,
                    )
                except Exception:
                    if failure is None:
                        raise
                    logger.warning("Could not confirm operation cleanup", exc_info=True)
