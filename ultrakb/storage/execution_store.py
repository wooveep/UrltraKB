"""Durable execution and stage boundaries, separate from publication transactions."""

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ultrakb.locks import LockCancelled
from ultrakb.stage_observations import StageObservation, encode_observation

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


def _application_identity(task_id, unit_id=None, parent_task_id=None):
    import re

    for value in (task_id, parent_task_id):
        if value is not None and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
        ):
            raise ValueError("Application task identities must be 32 lowercase hex characters")
    if unit_id is not None and (
        not isinstance(unit_id, str) or re.fullmatch(r"[0-9]{1,5}", unit_id) is None
    ):
        raise ValueError("Application work-unit identities must be bounded numeric ordinals")
    if task_id is None and (unit_id is not None or parent_task_id is not None):
        raise ValueError("An application work unit or retry requires its task identity")


def _application_budget(connection, task_id, unit_ordinal=""):
    if task_id is None:
        return None
    return connection.execute(
        "SELECT b.model_budget_id, b.deadline_at FROM ukb_application_budget_bindings a "
        "JOIN ukb_model_budgets b ON b.model_budget_id = a.model_budget_id "
        "WHERE a.application_task_id = ? AND a.unit_ordinal = ?",
        (task_id, unit_ordinal),
    ).fetchone()


def _application_continuation(connection, task_id, unit_id, parent_task_id, resume):
    unit = _application_budget(connection, task_id, unit_id or "")
    if resume is not None:
        return unit, False
    batch = _application_budget(connection, task_id)
    if unit is not None:
        return unit, batch is not None and unit[0] == batch[0]
    batch = batch or _application_budget(connection, parent_task_id)
    if batch is None and parent_task_id is not None:
        if connection.execute(
            "SELECT 1 FROM ukb_application_budget_bindings WHERE application_task_id = ? LIMIT 1",
            (parent_task_id,),
        ).fetchone():
            raise ValueError("Retrying mixed source budgets requires the original unit execution")
    return batch, True


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
        application_task_id: str | None = None,
        application_unit_id: str | None = None,
        parent_application_task_id: str | None = None,
    ) -> Execution:
        _label(operation)
        _label(fingerprint)
        _application_identity(application_task_id, application_unit_id, parent_application_task_id)
        if max_calls is not None and (type(max_calls) is not int or not 0 <= max_calls < 2**63):
            raise ValueError("Model-call limits must be nonnegative integers")
        if deadline_at is not None and not math.isfinite(deadline_at):
            raise ValueError("A model deadline must be finite")
        timestamp, monotonic, clock_id = now(), time.monotonic(), _clock_id()
        task_id, budget_id, execution_id = uuid4().hex, uuid4().hex, uuid4().hex
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            binding, share_budget = (
                _application_continuation(
                    connection,
                    application_task_id,
                    application_unit_id,
                    parent_application_task_id,
                    resume,
                )
                if operation == "model_task"
                else (None, False)
            )
            if binding is not None and resume is None:
                resume = connection.execute(
                    "SELECT execution_id FROM ukb_executions WHERE model_budget_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (binding[0],),
                ).fetchone()[0]
                # Another process may have bound this task after the caller's read.
                # The first execution's deadline remains authoritative.
                deadline_at = binding[1]
            if pending_attempt_id is not None:
                claim = connection.execute(
                    "SELECT i.status, i.attempt_id, a.status, g.generation, g.cancelled, "
                    "a.stop_requested "
                    "FROM ukb_pending_attempts a JOIN ukb_import_intents i "
                    "ON i.intent_id = a.intent_id JOIN ukb_import_groups g "
                    "ON g.root_import_id = i.root_import_id WHERE a.attempt_id = ?",
                    (pending_attempt_id,),
                ).fetchone()
                if (
                    claim
                    and claim[:4]
                    == ("started", pending_attempt_id, "started", self.identity.generation)
                    and any(claim[4:])
                ):
                    raise LockCancelled("The pending execution was stopped before starting")
                if claim != (
                    "started",
                    pending_attempt_id,
                    "started",
                    self.identity.generation,
                    0,
                    0,
                ):
                    raise ValueError("Execution requires its exact current pending claim")
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
                if binding is not None and previous[1] != binding[0]:
                    raise ValueError(
                        "An application work unit cannot replace its retained model budget"
                    )
                if previous[2:5] != (fingerprint, max_calls, deadline_at):
                    raise ValueError("Continuing an execution cannot reset policy or its budget")
                latest = connection.execute(
                    "SELECT state FROM ukb_executions WHERE task_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (previous[0],),
                ).fetchone()
                if (
                    previous[5] in {"started", "stopping"}
                    or latest[0] in {"started", "stopping"}
                    or previous[6] != operation
                ):
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
            if operation == "model_task" and application_task_id is not None:
                # Empty ordinal is the budget for new work in this batch. Explicit
                # source retries keep their own budgets without retargeting it.
                ordinals = {application_unit_id or ""} | ({""} if share_budget else set())
                for ordinal in sorted(ordinals):
                    connection.execute(
                        "INSERT INTO ukb_application_budget_bindings VALUES (?, ?, ?) "
                        "ON CONFLICT(application_task_id, unit_ordinal) DO NOTHING",
                        (application_task_id, ordinal, budget_id),
                    )
            connection.execute(
                "INSERT INTO ukb_executions (execution_id, task_id, model_budget_id, generation, "
                "fingerprint, pending_attempt_id, application_task_id, application_unit_id, "
                "parent_application_task_id, process_clock_id, started_at, "
                "started_monotonic, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started')",
                (
                    execution_id,
                    task_id,
                    budget_id,
                    self.identity.generation,
                    fingerprint,
                    pending_attempt_id,
                    application_task_id,
                    application_unit_id,
                    parent_application_task_id,
                    clock_id,
                    timestamp,
                    monotonic,
                ),
            )
            if pending_attempt_id is not None:
                connection.execute(
                    "UPDATE ukb_pending_attempts SET execution_id = ? WHERE attempt_id = ?",
                    (execution_id, pending_attempt_id),
                )
        return Execution(task_id, execution_id, budget_id)

    def application_execution(
        self, task_id: str, *, unit_ordinal: str | None = None
    ) -> Execution | None:
        """Resolve a batch or exact unit budget without scanning execution history."""
        _application_identity(task_id, unit_ordinal)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            binding = _application_budget(db.connection, task_id, unit_ordinal or "")
            if binding is None:
                return None
            row = db.connection.execute(
                "SELECT task_id, execution_id, model_budget_id FROM ukb_executions "
                "WHERE model_budget_id = ? ORDER BY sequence DESC LIMIT 1",
                (binding[0],),
            ).fetchone()
            return Execution(*row)

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

    def source_execution(self, source_revision_id: str) -> str | None:
        """Resume processing ownership; admission-only observations do not replace it."""
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT s.execution_id FROM ukb_stage_attempts s JOIN ukb_executions e "
                "ON e.execution_id = s.execution_id JOIN ukb_tasks t ON t.task_id = e.task_id "
                "WHERE s.source_revision_id = ? AND t.operation = 'model_task' "
                "AND s.stage_code != 'import.admit.v1' "
                "ORDER BY s.sequence DESC LIMIT 1",
                (source_revision_id,),
            ).fetchone()
            if row is None:
                return None
            latest = db.connection.execute(
                "SELECT execution_id FROM ukb_executions WHERE task_id = "
                "(SELECT task_id FROM ukb_executions WHERE execution_id = ?) "
                "ORDER BY sequence DESC LIMIT 1",
                (row[0],),
            ).fetchone()
            return latest[0]

    def pending_execution(self, intent_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT e.execution_id FROM ukb_executions e JOIN ukb_pending_attempts a "
                "ON a.attempt_id = e.pending_attempt_id JOIN ukb_tasks t ON t.task_id = e.task_id "
                "WHERE a.intent_id = ? AND t.operation = 'model_task' "
                "ORDER BY e.sequence DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
            return row[0] if row else None

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
        observation: StageObservation | None = None,
    ) -> str:
        _label(stage_code, limit=64)
        encoded = encode_observation(observation)
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
                "process_clock_id, started_at, started_monotonic, state, observation, "
                "observation_coverage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)",
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
                    encoded,
                    "pending" if encoded is not None else "not_recorded",
                ),
            )
        return stage_id

    def finish_stage(
        self,
        stage_id: str,
        *,
        state: str,
        error_code: str | None = None,
        observation: StageObservation | None = None,
    ):
        if state not in {"completed", "failed", "cancelled", "interrupted", "reused"}:
            raise ValueError("Invalid terminal stage state")
        if error_code is not None:
            _label(error_code)
        encoded = encode_observation(observation)
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
                "error_code = ?, observation = COALESCE(?, observation), "
                "observation_coverage = CASE WHEN ? THEN 'recorded' "
                "WHEN observation_coverage = 'pending' THEN 'partial' "
                "ELSE observation_coverage END WHERE stage_attempt_id = ?",
                (
                    state,
                    timestamp if state != "interrupted" else None,
                    duration,
                    error_code,
                    encoded,
                    int(encoded is not None),
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
