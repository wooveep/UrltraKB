"""Actual process clocks and native stages for compiler and model work."""

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

_CLOCK_ID = uuid.uuid4().hex


@dataclass(frozen=True)
class ObservationStart:
    """An actual in-process instant; never reconstructed from old file metadata."""

    event_id: str
    utc: str
    monotonic_ns: int
    clock_id: str


def start_observation() -> ObservationStart:
    return ObservationStart(
        uuid.uuid4().hex,
        datetime.now(timezone.utc).isoformat(),
        time.monotonic_ns(),
        f"{os.getpid()}:{_CLOCK_ID}",
    )


@contextmanager
def phase_observation(event: str, *, stage: str, work_id: str | None = None):
    """Keep optional queue/compiler detail without replacing a business failure."""
    import logging
    from contextlib import nullcontext

    from ultrakb.application.execution import current_execution
    from ultrakb.llm_usage_transport import _failure_state
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.stage_context import bind_stage, parent_stage

    context = current_execution()
    if context is None or context.storage_execution is None or context.snapshot is None:
        yield uuid.uuid4().hex
        return
    from pathlib import Path

    root, identity = Path(context.snapshot.kb_dir), context.storage_identity
    assert identity is not None
    store = ExecutionStore(root, identity)
    execution_id = context.storage_execution.execution_id
    code = {"queue": "model.queue.v1", "slot": "model.slot.v1"}.get(event, f"compiler.{event}.v1")
    stage_id = None
    try:
        stage_id = store.start_stage(
            execution_id,
            code,
            work_key=work_id or stage,
            parent_stage_id=parent_stage(root, identity, execution_id),
        )
    except Exception:
        logging.getLogger(__name__).warning("Stage detail unavailable", exc_info=True)
    failure = None
    try:
        with bind_stage(root, identity, execution_id, stage_id) if stage_id else nullcontext():
            yield stage_id or uuid.uuid4().hex
    except BaseException as error:
        failure = error
        raise
    finally:
        if stage_id is not None:
            try:
                store.finish_stage(
                    stage_id,
                    state=_failure_state(failure) if failure else "completed",
                    error_code=type(failure).__name__ if failure else None,
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Stage detail remains incomplete", exc_info=True
                )
