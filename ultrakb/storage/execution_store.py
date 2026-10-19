"""Durable execution and stage boundaries, separate from publication transactions."""

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ultrakb.locks import LockCancelled

from .coordination import storage_stage
from .database import DatabaseIdentity

_PROCESS_CLOCK = (os.getpid(), uuid4().hex)


def _clock_id():
    global _PROCESS_CLOCK
    if _PROCESS_CLOCK[0] != os.getpid():
        _PROCESS_CLOCK = (os.getpid(), uuid4().hex)
    return _PROCESS_CLOCK[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def execution_state(connection, identity, execution_id):
    row = connection.execute(
        "SELECT generation, state FROM ukb_executions WHERE execution_id = ?", (execution_id,)
    ).fetchone()
    if row is None or row[0] != identity.generation:
        raise ValueError("The execution belongs to another generation")
    return row[1]


def require_active_execution(connection, identity, execution_id):
    state = execution_state(connection, identity, execution_id)
    if state in {"stopping", "cancelled"}:
        raise LockCancelled("The execution was stopped")
    if state != "started":
        raise ValueError("A terminated execution cannot accept late results")


def _label(value, *, limit=256):
    if not isinstance(value, str) or not value or len(value.encode()) > limit:
        raise ValueError("Execution metadata must be nonempty and bounded")


@dataclass(frozen=True)
class Execution:
    task_id: str
    execution_id: str
    model_budget_id: str


@dataclass(frozen=True)
class ModelBudget:
    model_budget_id: str
    max_calls: int | None
    used: int
    deadline_at: float | None


class ExecutionStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def begin(
        self,
        operation: str,
        *,
        fingerprint: str,
        max_calls: int | None,
        deadline_at: float | None = None,
        resume: str | None = None,
        pending_attempt_id: str | None = None,
    ) -> Execution:
        _label(operation)
        _label(fingerprint)
        if max_calls is not None and (type(max_calls) is not int or not 0 <= max_calls < 2**63):
            raise ValueError("Model-call limits must be nonnegative integers")
        if deadline_at is not None and not math.isfinite(deadline_at):
            raise ValueError("A model deadline must be finite")
        timestamp, monotonic, clock_id = now(), time.monotonic(), _clock_id()
        task_id, budget_id, execution_id = uuid4().hex, uuid4().hex, uuid4().hex
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            if resume:
                previous = connection.execute(
                    "SELECT e.task_id, e.model_budget_id, b.fingerprint, b.max_calls, "
                    "b.deadline_at, e.state, t.operation FROM ukb_executions e "
                    "JOIN ukb_model_budgets b ON b.model_budget_id = e.model_budget_id "
                    "JOIN ukb_tasks t ON t.task_id = e.task_id WHERE e.execution_id = ?",
                    (resume,),
                ).fetchone()
                if not previous:
                    raise KeyError(resume)
                if previous[2:5] != (fingerprint, max_calls, deadline_at):
                    raise ValueError("Continuing an execution cannot reset policy or its budget")
                if previous[5] in {"started", "stopping"} or previous[6] != operation:
                    raise ValueError("Finish or recover the original execution before continuing")
                task_id, budget_id = previous[:2]
            else:
                connection.execute(
                    "INSERT INTO ukb_tasks(task_id, operation, created_at) VALUES (?, ?, ?)",
                    (task_id, operation, timestamp),
                )
                connection.execute(
                    "INSERT INTO ukb_model_budgets VALUES (?, ?, ?, ?, 0, ?)",
                    (budget_id, task_id, fingerprint, max_calls, deadline_at),
                )
            connection.execute(
                "INSERT INTO ukb_executions (execution_id, task_id, model_budget_id, generation, "
                "fingerprint, pending_attempt_id, process_clock_id, started_at, "
                "started_monotonic, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started')",
                (
                    execution_id,
                    task_id,
                    budget_id,
                    self.identity.generation,
                    fingerprint,
                    pending_attempt_id,
                    clock_id,
                    timestamp,
                    monotonic,
                ),
            )
        return Execution(task_id, execution_id, budget_id)

    def budget(self, model_budget_id: str) -> ModelBudget:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT model_budget_id, max_calls, used, deadline_at FROM ukb_model_budgets "
                "WHERE model_budget_id = ?",
                (model_budget_id,),
            ).fetchone()
            if not row:
                raise KeyError(model_budget_id)
            return ModelBudget(*row)

    def execution(self, execution_id: str) -> Execution:
        """Read a retained execution identity; begin still validates policy and state."""
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT task_id, execution_id, model_budget_id FROM ukb_executions "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            return Execution(*row)

    def status(self, execution_id: str) -> str:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT state FROM ukb_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            return row[0]

    def start_stage(
        self,
        execution_id: str,
        stage_code: str,
        *,
        parent_stage_id: str | None = None,
        work_key: str | None = None,
        ordinal: int | None = None,
        source_revision_id: str | None = None,
        unit_revision_id: str | None = None,
        allow_stopping: bool = False,
    ) -> str:
        _label(stage_code, limit=64)
        if work_key is not None:
            _label(work_key, limit=1024)
        if ordinal is not None and (type(ordinal) is not int or not 0 <= ordinal < 2**63):
            raise ValueError("Work ordinals must be nonnegative integers")
        stage_id, timestamp, monotonic = uuid4().hex, now(), time.monotonic()
        clock_id = _clock_id()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            if type(allow_stopping) is not bool:
                raise ValueError("Cleanup permission must be explicit")
            state = execution_state(connection, self.identity, execution_id)
            if state != "stopping" or not allow_stopping:
                require_active_execution(connection, self.identity, execution_id)
            if parent_stage_id is not None:
                parent = connection.execute(
                    "SELECT execution_id, state FROM ukb_stage_attempts WHERE stage_attempt_id = ?",
                    (parent_stage_id,),
                ).fetchone()
                if parent != (execution_id, "started"):
                    raise ValueError("A nested stage requires an active parent in this execution")
            connection.execute(
                "INSERT INTO ukb_stage_attempts(stage_attempt_id, execution_id, parent_stage_id, "
                "stage_code, work_key, ordinal, source_revision_id, unit_revision_id, "
                "process_clock_id, started_at, started_monotonic, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started')",
                (
                    stage_id,
                    execution_id,
                    parent_stage_id,
                    stage_code,
                    work_key,
                    ordinal,
                    source_revision_id,
                    unit_revision_id,
                    clock_id,
                    timestamp,
                    monotonic,
                ),
            )
        return stage_id

    def finish_stage(self, stage_id: str, *, state: str, error_code: str | None = None):
        if state not in {"completed", "failed", "cancelled", "interrupted", "reused"}:
            raise ValueError("Invalid terminal stage state")
        if error_code is not None:
            _label(error_code)
        timestamp, monotonic, clock_id = now(), time.monotonic(), _clock_id()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            row = connection.execute(
                "SELECT s.process_clock_id, s.started_monotonic, s.state, e.generation "
                "FROM ukb_stage_attempts s JOIN ukb_executions e "
                "ON e.execution_id = s.execution_id WHERE s.stage_attempt_id = ?",
                (stage_id,),
            ).fetchone()
            if not row:
                raise KeyError(stage_id)
            if row[2] != "started" or row[3] != self.identity.generation:
                return False
            # Interruption has no observed terminal instant. Different process clocks
            # must never be subtracted even when they happen to use the same epoch.
            duration = monotonic - row[1] if row[0] == clock_id and state != "interrupted" else None
            connection.execute(
                "UPDATE ukb_stage_attempts SET state = ?, ended_at = ?, duration_seconds = ?, "
                "error_code = ? WHERE stage_attempt_id = ?",
                (
                    state,
                    timestamp if state != "interrupted" else None,
                    duration,
                    error_code,
                    stage_id,
                ),
            )
            return True

    def request_stop(self, execution_id: str) -> bool:
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            return bool(
                connection.execute(
                    "UPDATE ukb_executions SET state = 'stopping' WHERE execution_id = ? "
                    "AND state = 'started' AND generation = ?",
                    (execution_id, self.identity.generation),
                ).rowcount
            )

    def finish(self, execution_id: str, *, state: str, work_settled: bool = False) -> bool:
        """The controller can attest settled work despite missing stage observations.

        An open stage otherwise blocks termination. This attestation belongs only
        after actual SDK/worker cleanup, never to a timeout or a stop request.
        """
        if state not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Invalid terminal execution state")
        if type(work_settled) is not bool:
            raise ValueError("Execution cleanup requires an explicit settled-work attestation")
        timestamp, monotonic, clock_id = now(), time.monotonic(), _clock_id()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            row = connection.execute(
                "SELECT process_clock_id, started_monotonic, state, generation "
                "FROM ukb_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise KeyError(execution_id)
            if row[2] not in {"started", "stopping"} or row[3] != self.identity.generation:
                return False
            if (
                not work_settled
                and connection.execute(
                    "SELECT 1 FROM ukb_stage_attempts WHERE execution_id = ? AND state = 'started' "
                    "LIMIT 1",
                    (execution_id,),
                ).fetchone()
            ):
                raise ValueError("Execution still has active stages; wait for worker cleanup")
            duration = monotonic - row[1] if row[0] == clock_id and state != "interrupted" else None
            connection.execute(
                "UPDATE ukb_executions SET state = ?, ended_at = ?, duration_seconds = ? "
                "WHERE execution_id = ?",
                (state, timestamp if state != "interrupted" else None, duration, execution_id),
            )
            return True
