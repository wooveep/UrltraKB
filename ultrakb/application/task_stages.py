"""Durable actual non-model boundaries, independent of UI progress subscription."""

import logging
from contextlib import contextmanager
from pathlib import Path

from ultrakb.locks import LockCancelled
from ultrakb.stage_observations import StageObservation
from ultrakb.storage.costs import capture_costs
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.stage_context import bind_stage, parent_stage

logger = logging.getLogger(__name__)


@contextmanager
def task_stage(
    context,
    code,
    *,
    root=None,
    ensure_work=False,
    allow_stopping=False,
    observation_input=None,
    **identity,
):
    execution = context.storage_execution
    if execution is None:
        raise RuntimeError("A durable stage requires its top-level execution first")
    if root is None and context.snapshot is None:
        raise ValueError("A non-model stage requires its explicit database root")
    root = Path(root if root is not None else context.snapshot.kb_dir)
    store = ExecutionStore(root, context.storage_identity)
    initial = (
        StageObservation(input=observation_input)
        if context.observe_storage_costs or observation_input is not None
        else None
    )
    try:
        with capture_costs(False):
            stage_id = store.start_stage(
                execution.execution_id,
                code,
                allow_stopping=allow_stopping,
                parent_stage_id=parent_stage(
                    root, context.storage_identity, execution.execution_id
                ),
                observation=initial,
                **identity,
            )
    except Exception:
        if not ensure_work:
            raise
        # Missing observations must never skip actual SDK/worker cleanup.
        yield {}
        raise
    failure, capture = None, None
    progress = {}
    try:
        with (
            capture_costs(context.observe_storage_costs) as capture,
            bind_stage(root, context.storage_identity, execution.execution_id, stage_id),
        ):
            entered = False
            try:
                with context.activity(code) as progress:
                    entered = True
                    progress["stage_attempt_id"] = stage_id
                    progress["execution_id"] = execution.execution_id
                    yield progress
            except BaseException:
                if not ensure_work or entered:
                    raise
                # A subscriber can fail while entering, before the body is reached.
                yield {"stage_attempt_id": stage_id, "execution_id": execution.execution_id}
                raise
    except BaseException as error:
        failure = error
        raise
    finally:
        if failure is None:
            state = (
                progress["status"]
                if progress.get("status") in {"failed", "cancelled", "interrupted", "reused"}
                else "completed"
            )
        else:
            state = (
                "cancelled"
                if isinstance(failure, (LockCancelled, GeneratorExit))
                or type(failure).__name__ == "CancelledError"
                else "failed"
            )
        try:
            observed = (
                StageObservation(
                    input=initial.input if initial else None,
                    storage=capture.finish() if capture is not None else None,
                )
                if initial is not None
                else None
            )
            with capture_costs(False):
                store.finish_stage(
                    stage_id,
                    state=state,
                    error_code=type(failure).__name__ if failure else progress.get("reason_code"),
                    observation=observed,
                )
        except Exception:
            if failure is None:
                raise
            logger.warning("Could not confirm stage termination", exc_info=True)
