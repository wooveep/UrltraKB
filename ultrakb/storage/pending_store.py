"""Persistent import work: stable targets, append-only attempts, and group budgets."""

from dataclasses import dataclass
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


_WORK_COLUMNS = (
    "i.sequence, i.intent_id, i.root_import_id, i.source_revision_id, "
    "i.unit_revision_id, i.kind, i.status, i.dispatch_id, i.attempt_id"
)
_RETRYABLE = {"failed", "blocked", "interrupted", "stopped"}


def _page(after: int, limit: int):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Work pages require a nonnegative cursor and a limit of 1..1000")


def _current(connection, source_revision_id, unit_revision_id):
    source = connection.execute(
        "SELECT source_id, ordinal FROM ukb_source_revisions "
        "WHERE revision_id = ? AND state = 'ready'",
        (source_revision_id,),
    ).fetchone()
    if not source:
        raise KeyError(source_revision_id)
    latest = connection.execute(
        "SELECT revision_id FROM ukb_source_revisions WHERE source_id = ? AND state = 'ready' "
        "ORDER BY ordinal DESC LIMIT 1",
        (source[0],),
    ).fetchone()[0]
    if latest != source_revision_id:
        raise CatalogConflict("The source target changed before pending work could start")
    if unit_revision_id is not None:
        unit = connection.execute(
            "SELECT u.target_revision_id, p.source_revision_id FROM ukb_unit_revisions r "
            "JOIN ukb_import_units u ON u.unit_id = r.unit_id "
            "JOIN ukb_processing_versions p ON p.processing_id = r.processing_id "
            "WHERE r.unit_revision_id = ?",
            (unit_revision_id,),
        ).fetchone()
        if not unit:
            raise KeyError(unit_revision_id)
        if unit != (unit_revision_id, source_revision_id):
            raise CatalogConflict("The processing-unit target changed before pending work")


class PendingStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

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
            _current(connection, source_revision_id, unit_revision_id)
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

    def claim(self, *, intent_id: str | None = None) -> PendingWork | None:
        if intent_id is not None:
            TypeAdapter(RecordId).validate_python(intent_id)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            row = connection.execute(
                f"SELECT {_WORK_COLUMNS} FROM ukb_import_intents i "
                "JOIN ukb_import_groups g ON g.root_import_id = i.root_import_id "
                "WHERE i.status = 'pending' AND g.cancelled = 0 AND g.generation = ? "
                "AND (? IS NULL OR i.intent_id = ?) "
                "ORDER BY i.sequence LIMIT 1",
                (self.identity.generation, intent_id, intent_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE ukb_import_intents SET status = 'dispatched', dispatch_id = ? "
                "WHERE intent_id = ? AND status = 'pending'",
                (uuid4().hex, row[1]),
            )
            return self._job(connection, row[1])

    def start(self, claimed: PendingWork | None) -> PendingWork | None:
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
                return work
            if work.status != "dispatched":
                return None
            try:
                _current(connection, work.source_revision_id, work.unit_revision_id)
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
        status: Literal["completed", "failed", "blocked", "interrupted", "stopped"],
        publication_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        if status not in _RETRYABLE | {"completed"}:
            raise ValueError("Only a terminal execution outcome can finish an attempt")
        if error_code is not None and (not error_code or len(error_code.encode()) > 256):
            raise ValueError("A bounded diagnostic code is required")
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
                "finished_at = ? WHERE attempt_id = ? AND status = 'started'",
                (status, publication_id, error_code, timestamp, work.attempt_id),
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
            if group[0] or group[1] != self.identity.generation or work.status not in _RETRYABLE:
                raise CatalogConflict("This import cannot be retried in its current state")
            _current(connection, work.source_revision_id, work.unit_revision_id)
            connection.execute(
                "UPDATE ukb_import_intents SET status = 'pending', dispatch_id = NULL, "
                "attempt_id = NULL WHERE intent_id = ?",
                (intent_id,),
            )
            return self._job(connection, intent_id)

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
                "started_at, finished_at FROM ukb_pending_attempts WHERE intent_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (intent_id, after, limit),
            ).fetchall()
            return tuple(WorkAttempt(*row) for row in rows)
