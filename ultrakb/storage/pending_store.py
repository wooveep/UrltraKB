"""Persistent import work: stable targets, append-only attempts, and group budgets."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from ultrakb.pending.records import ExecutionBudget
from ultrakb.source_records import RecordId

from .catalog_store import CatalogConflict
from .coordination import storage_stage
from .database import DatabaseIdentity


@dataclass(frozen=True)
class PendingWork:
    sequence: int
    intent_id: str
    root_import_id: str
    source_revision_id: str
    unit_revision_id: str | None
    kind: str
    status: str
    dispatch_id: str | None
    attempt_id: str | None


@dataclass(frozen=True)
class WorkAttempt:
    sequence: int
    attempt_id: str
    intent_id: str
    status: str
    publication_id: str | None
    error_code: str | None
    started_at: str
    finished_at: str | None
    message: str | None = None


_WORK_COLUMNS = (
    "i.sequence, i.intent_id, i.root_import_id, i.source_revision_id, "
    "i.unit_revision_id, i.kind, i.status, i.dispatch_id, i.attempt_id"
)
_RETRYABLE = {"failed", "blocked", "interrupted", "stopped", "partial", "budget_wait"}


def _page(after: int, limit: int):
    if (
        type(after) is not int
        or not 0 <= after < 2**63
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise ValueError("Work pages require a nonnegative cursor and a limit of 1..1000")


def _current(connection, source_revision_id, unit_revision_id):
    source = connection.execute(
        "SELECT source_id, ordinal FROM ukb_source_revisions "
        "WHERE revision_id = ? AND state = 'ready'",
        (source_revision_id,),
    ).fetchone()
    if not source:
        raise KeyError(source_revision_id)
    if connection.execute(
        "SELECT state, contribution_empty FROM ukb_sources WHERE source_id = ?", (source[0],)
    ).fetchone() != ("active", 0):
        raise CatalogConflict("The source contribution was excluded before pending work")
    latest = connection.execute(
        "SELECT revision_id FROM ukb_source_revisions WHERE source_id = ? AND state = 'ready' "
        "ORDER BY ordinal DESC LIMIT 1",
        (source[0],),
    ).fetchone()[0]
    if latest != source_revision_id:
        raise CatalogConflict("The source target changed before pending work could start")
    if unit_revision_id is not None:
        unit = connection.execute(
            "SELECT u.target_revision_id, p.source_revision_id, p.reprocessing_request_id "
            "FROM ukb_unit_revisions r "
            "JOIN ukb_import_units u ON u.unit_id = r.unit_id "
            "JOIN ukb_processing_versions p ON p.processing_id = r.processing_id "
            "WHERE r.unit_revision_id = ?",
            (unit_revision_id,),
        ).fetchone()
        if not unit:
            raise KeyError(unit_revision_id)
        if unit[:2] != (unit_revision_id, source_revision_id):
            raise CatalogConflict("The processing-unit target changed before pending work")
        from .reprocessing_store import require_current_reprocessing

        require_current_reprocessing(connection, source_revision_id, unit[2])


def require_work_input(connection, work):
    if work.kind == "discovery":
        if (
            connection.execute(
                "SELECT 1 FROM ukb_source_revisions WHERE revision_id = ? AND state = 'ready'",
                (work.source_revision_id,),
            ).fetchone()
            is None
        ):
            raise CatalogConflict("The frozen discovery original is unavailable")
        return
    if (
        work.unit_revision_id is None
        and connection.execute(
            "SELECT 1 FROM ukb_discovery_objects WHERE import_intent_id = ?", (work.intent_id,)
        ).fetchone()
    ):
        bound = connection.execute(
            "SELECT source_revision_id FROM ukb_pending_source_bindings WHERE intent_id = ?",
            (work.intent_id,),
        ).fetchone()
        if bound:
            _current(connection, bound[0], None)
        return
    _current(connection, work.source_revision_id, work.unit_revision_id)


def require_running_work(connection, identity, work, *, allow_cancelled=False, check_input=True):
    row = connection.execute(
        "SELECT i.status, i.dispatch_id, i.attempt_id, g.generation, g.cancelled "
        "FROM ukb_import_intents i JOIN ukb_import_groups g "
        "ON g.root_import_id = i.root_import_id WHERE i.intent_id = ?",
        (work.intent_id,),
    ).fetchone()
    if row is None or row[:4] != (
        "started",
        work.dispatch_id,
        work.attempt_id,
        identity.generation,
    ):
        raise CatalogConflict("The pending execution claim changed")
    stopped = connection.execute(
        "SELECT stop_requested FROM ukb_pending_attempts WHERE attempt_id = ?",
        (work.attempt_id,),
    ).fetchone()
    if (row[4] or stopped == (1,)) and not allow_cancelled:
        from ultrakb.locks import LockCancelled

        raise LockCancelled("The pending execution was stopped or its import group was cancelled")
    if check_input:
        require_work_input(connection, work)


class PendingStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def unit_status(self, unit_revision_id):
        """Read the last actual work outcome for one immutable retained target."""
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT i.status, a.error_code FROM ukb_import_intents i "
                "LEFT JOIN ukb_pending_attempts a ON a.attempt_id = i.attempt_id "
                "WHERE i.unit_revision_id = ? AND i.kind = 'import' "
                "ORDER BY i.sequence DESC LIMIT 1",
                (unit_revision_id,),
            ).fetchone()
            return row

    def enqueue(
        self,
        source_revision_id: str,
        *,
        unit_revision_id: str | None = None,
        root_import_id: str | None = None,
        kind: Literal["import", "discovery"] = "import",
        budget: ExecutionBudget | None = None,
    ) -> PendingWork:
        for identity in (source_revision_id, unit_revision_id, root_import_id):
            if identity is not None:
                TypeAdapter(RecordId).validate_python(identity)
        if kind not in {"import", "discovery"}:
            raise ValueError("Unknown pending-work kind")
        budget_json = (budget or ExecutionBudget()).model_dump_json()
        root_import_id = root_import_id or uuid4().hex
        intent_id = uuid4().hex
        work_key = f"{kind}:{unit_revision_id or source_revision_id}"
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            if kind != "discovery":
                _current(connection, source_revision_id, unit_revision_id)
            elif (
                connection.execute(
                    "SELECT 1 FROM ukb_source_revisions WHERE revision_id = ? AND state = 'ready'",
                    (source_revision_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(source_revision_id)
            group = connection.execute(
                "SELECT generation, budget_json, cancelled FROM ukb_import_groups "
                "WHERE root_import_id = ?",
                (root_import_id,),
            ).fetchone()
            if group:
                if group[0] != self.identity.generation or group[2]:
                    raise CatalogConflict("The import group is cancelled or belongs to another run")
                if budget is not None and group[1] != budget_json:
                    raise CatalogConflict("Continuing an import cannot reset its original budget")
            else:
                connection.execute(
                    "INSERT INTO ukb_import_groups VALUES (?, ?, ?, 0)",
                    (root_import_id, self.identity.generation, budget_json),
                )
            connection.execute(
                "INSERT INTO ukb_import_intents "
                "(intent_id, root_import_id, source_revision_id, unit_revision_id, work_key, "
                "kind, status) VALUES (?, ?, ?, ?, ?, ?, 'pending') "
                "ON CONFLICT(root_import_id, work_key) DO NOTHING",
                (intent_id, root_import_id, source_revision_id, unit_revision_id, work_key, kind),
            )
            row = connection.execute(
                f"SELECT {_WORK_COLUMNS} FROM ukb_import_intents i "
                "WHERE root_import_id = ? AND work_key = ?",
                (root_import_id, work_key),
            ).fetchone()
            return PendingWork(*row)

    def claim(self, *, intent_id: str | None = None, cancelled=None) -> PendingWork | None:
        if intent_id is not None:
            TypeAdapter(RecordId).validate_python(intent_id)
        with (
            storage_stage(self.root, self.identity, cancelled=cancelled) as db,
            db.transaction() as connection,
        ):
            row = connection.execute(
                f"SELECT {_WORK_COLUMNS} FROM ukb_import_intents i "
                "JOIN ukb_import_groups g ON g.root_import_id = i.root_import_id "
                "WHERE i.status IN ('pending', 'dispatched') AND g.cancelled = 0 "
                "AND g.generation = ? "
                "AND (? IS NULL OR i.intent_id = ?) "
                "ORDER BY i.sequence LIMIT 1",
                (self.identity.generation, intent_id, intent_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE ukb_import_intents SET status = 'dispatched', dispatch_id = ? "
                "WHERE intent_id = ? AND status IN ('pending', 'dispatched')",
                (uuid4().hex, row[1]),
            )
            return self._job(connection, row[1])

    def start(self, claimed: PendingWork | None, *, new_only=False) -> PendingWork | None:
        if claimed is None:
            return None
        timestamp = datetime.now(timezone.utc).isoformat()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            work = self._job(connection, claimed.intent_id)
            if work.dispatch_id != claimed.dispatch_id or not work.dispatch_id:
                return None
            cancelled = connection.execute(
                "SELECT cancelled, generation FROM ukb_import_groups WHERE root_import_id = ?",
                (work.root_import_id,),
            ).fetchone()
            if cancelled[0] or cancelled[1] != self.identity.generation:
                return None
            if work.status == "started":
                return None if new_only else work
            if work.status != "dispatched":
                return None
            try:
                require_work_input(connection, work)
            except CatalogConflict:
                connection.execute(
                    "UPDATE ukb_import_intents SET status = 'stale' WHERE intent_id = ?",
                    (work.intent_id,),
                )
                return None
            attempt_id = uuid4().hex
            connection.execute(
                "INSERT INTO ukb_pending_attempts "
                "(attempt_id, intent_id, dispatch_id, generation, status, started_at) "
                "VALUES (?, ?, ?, ?, 'started', ?)",
                (
                    attempt_id,
                    work.intent_id,
                    work.dispatch_id,
                    self.identity.generation,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE ukb_import_intents SET status = 'started', attempt_id = ? "
                "WHERE intent_id = ?",
                (attempt_id, work.intent_id),
            )
            return self._job(connection, work.intent_id)

    def finish(
        self,
        work: PendingWork,
        *,
        status: Literal[
            "completed", "failed", "blocked", "interrupted", "stopped", "partial", "budget_wait"
        ],
        publication_id: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> bool:
        if status not in _RETRYABLE | {"completed"}:
            raise ValueError("Only a terminal execution outcome can finish an attempt")
        if error_code is not None and (not error_code or len(error_code.encode()) > 256):
            raise ValueError("A bounded diagnostic code is required")
        if message is not None and len(message.encode()) > 4096:
            raise ValueError("Pending diagnostics must be bounded")
        if publication_id is not None:
            TypeAdapter(RecordId).validate_python(publication_id)
        if status == "completed" and work.kind == "import":
            raise ValueError("Import success must be committed in its publication transaction")
        timestamp = datetime.now(timezone.utc).isoformat()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            current = self._job(connection, work.intent_id)
            generation = connection.execute(
                "SELECT generation FROM ukb_pending_attempts WHERE attempt_id = ?",
                (work.attempt_id,),
            ).fetchone()
            if (
                current.status != "started"
                or current.attempt_id != work.attempt_id
                or current.dispatch_id != work.dispatch_id
                or generation != (self.identity.generation,)
            ):
                return False
            connection.execute(
                "UPDATE ukb_pending_attempts SET status = ?, publication_id = ?, error_code = ?, "
                "finished_at = ?, message = ? WHERE attempt_id = ? AND status = 'started'",
                (status, publication_id, error_code, timestamp, message, work.attempt_id),
            )
            connection.execute(
                "UPDATE ukb_import_intents SET status = ? WHERE intent_id = ?",
                (status, work.intent_id),
            )
            return True

    def retry(self, intent_id: str) -> PendingWork:
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            work = self._job(connection, intent_id)
            group = connection.execute(
                "SELECT cancelled, generation FROM ukb_import_groups WHERE root_import_id = ?",
                (work.root_import_id,),
            ).fetchone()
            if work.status == "started":
                running = connection.execute(
                    "SELECT e.state FROM ukb_executions e JOIN ukb_pending_attempts a "
                    "ON a.execution_id = e.execution_id WHERE a.attempt_id = ?",
                    (work.attempt_id,),
                ).fetchone()
                if running is None or running[0] not in {"started", "stopping"}:
                    connection.execute(
                        "UPDATE ukb_pending_attempts SET status = 'interrupted', "
                        "error_code = 'result_unconfirmed' WHERE attempt_id = ? "
                        "AND status = 'started'",
                        (work.attempt_id,),
                    )
                    work = replace(work, status="interrupted")
            if group[0] or group[1] != self.identity.generation or work.status not in _RETRYABLE:
                raise CatalogConflict("This import cannot be retried in its current state")
            require_work_input(connection, work)
            connection.execute(
                "UPDATE ukb_import_intents SET status = 'pending', dispatch_id = NULL, "
                "attempt_id = NULL WHERE intent_id = ?",
                (intent_id,),
            )
            return self._job(connection, intent_id)

    def bind_execution(self, work, execution):
        from .execution_store import require_active_execution

        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            require_running_work(connection, self.identity, work)
            require_active_execution(connection, self.identity, execution.execution_id)
            previous = connection.execute(
                "SELECT execution_id FROM ukb_pending_attempts WHERE attempt_id = ?",
                (work.attempt_id,),
            ).fetchone()[0]
            if previous not in (None, execution.execution_id):
                from .execution_store import execution_state

                if execution_state(connection, self.identity, previous) in {"started", "stopping"}:
                    raise CatalogConflict("The pending attempt belongs to another active execution")
            connection.execute(
                "UPDATE ukb_pending_attempts SET execution_id = ? WHERE attempt_id = ?",
                (execution.execution_id, work.attempt_id),
            )

    def stop(self, intent_id, dispatch_id):
        """Fence one delivery immediately; its worker records completion after cleanup."""
        TypeAdapter(RecordId).validate_python(intent_id)
        TypeAdapter(RecordId).validate_python(dispatch_id)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            work = self._job(connection, intent_id)
            if work.dispatch_id != dispatch_id or work.status not in {"dispatched", "started"}:
                return False
            if work.status == "dispatched":
                connection.execute(
                    "UPDATE ukb_import_intents SET status = 'stopped' WHERE intent_id = ?",
                    (intent_id,),
                )
            else:
                connection.execute(
                    "UPDATE ukb_pending_attempts SET stop_requested = 1 WHERE attempt_id = ?",
                    (work.attempt_id,),
                )
                connection.execute(
                    "UPDATE ukb_executions SET state = 'stopping' WHERE state = 'started' "
                    "AND execution_id = (SELECT execution_id FROM ukb_pending_attempts "
                    "WHERE attempt_id = ?)",
                    (work.attempt_id,),
                )
            return True

    def budget(self, root_import_id: str) -> ExecutionBudget:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT budget_json FROM ukb_import_groups WHERE root_import_id = ?",
                (root_import_id,),
            ).fetchone()
            if not row:
                raise KeyError(root_import_id)
            return ExecutionBudget.model_validate_json(row[0])

    def cancel(self, root_import_id: str):
        with storage_stage(self.root, self.identity) as db:
            with db.transaction() as connection:
                changed = connection.execute(
                    "UPDATE ukb_import_groups SET cancelled = 1 WHERE root_import_id = ? "
                    "AND generation = ?",
                    (root_import_id, self.identity.generation),
                ).rowcount
                if not changed:
                    raise KeyError(root_import_id)
            # Running workers acknowledge stop only after their real cleanup has finished.
            while True:
                with db.transaction() as connection:
                    executions = connection.execute(
                        "SELECT e.execution_id FROM ukb_executions e WHERE e.state = 'started' "
                        "AND (e.model_budget_id = ? OR EXISTS "
                        "(SELECT 1 FROM ukb_pending_attempts a JOIN ukb_import_intents i "
                        "ON i.intent_id = a.intent_id WHERE a.attempt_id = e.pending_attempt_id "
                        "AND i.root_import_id = ?) OR EXISTS (SELECT 1 FROM ukb_usage_requests r "
                        "WHERE r.execution_id = e.execution_id AND r.root_import_id = ?)) "
                        "ORDER BY e.sequence LIMIT 63",
                        (root_import_id, root_import_id, root_import_id),
                    ).fetchall()
                    for [execution_id] in executions:
                        connection.execute(
                            "UPDATE ukb_executions SET state = 'stopping' WHERE execution_id = ?",
                            (execution_id,),
                        )
                if len(executions) < 63:
                    break
            while True:
                with db.transaction() as connection:
                    ids = connection.execute(
                        "SELECT intent_id FROM ukb_import_intents WHERE root_import_id = ? "
                        "AND status IN ('pending', 'dispatched') ORDER BY sequence LIMIT 128",
                        (root_import_id,),
                    ).fetchall()
                    for (intent_id,) in ids:
                        connection.execute(
                            "UPDATE ukb_import_intents SET status = 'cancelled' "
                            "WHERE intent_id = ?",
                            (intent_id,),
                        )
                if len(ids) < 128:
                    break

    @staticmethod
    def _job(connection, intent_id):
        row = connection.execute(
            f"SELECT {_WORK_COLUMNS} FROM ukb_import_intents i WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if not row:
            raise KeyError(intent_id)
        return PendingWork(*row)

    def job(self, intent_id: str) -> PendingWork:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._job(db.connection, intent_id)

    def jobs(self, *, after: int = 0, limit: int = 100) -> tuple[PendingWork, ...]:
        _page(after, limit)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                f"SELECT {_WORK_COLUMNS} FROM ukb_import_intents i WHERE sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (after, limit),
            ).fetchall()
            return tuple(PendingWork(*row) for row in rows)

    def attempts(
        self, intent_id: str, *, after: int = 0, limit: int = 100
    ) -> tuple[WorkAttempt, ...]:
        _page(after, limit)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT sequence, attempt_id, intent_id, status, publication_id, error_code, "
                "started_at, finished_at, message FROM ukb_pending_attempts WHERE intent_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (intent_id, after, limit),
            ).fetchall()
            return tuple(WorkAttempt(*row) for row in rows)
