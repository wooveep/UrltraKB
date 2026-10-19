"""Bounded read models over authoritative execution, stage and usage records."""

from pydantic import TypeAdapter

from ultrakb.llm_usage import UsageRequest
from ultrakb.llm_usage_models import UsageAggregate
from ultrakb.source_records import RecordId
from ultrakb.task_records import (
    TaskBudget,
    TaskDetail,
    TaskExecution,
    TaskList,
    TaskOverview,
    TaskPublication,
    TaskStage,
)

from .coordination import storage_stage
from .execution_schema import COUNTS
from .execution_store import now
from .usage_store import summarize_counters

_OVERVIEW = (
    "t.sequence, t.task_id, t.operation, t.created_at, e.execution_id, e.state, "
    "e.started_at, e.ended_at, e.duration_seconds, e.fingerprint, e.model_budget_id, "
    "b.used, b.max_calls, b.deadline_at"
)
_LATEST = (
    "FROM ukb_tasks t JOIN ukb_executions e ON e.sequence = "
    "(SELECT max(sequence) FROM ukb_executions WHERE task_id = t.task_id) "
    "JOIN ukb_model_budgets b ON b.model_budget_id = e.model_budget_id "
)
_EXECUTION = (
    "sequence, execution_id, generation, state, started_at, ended_at, duration_seconds, fingerprint"
)
_EXECUTION_KEYS = (
    "execution_id",
    "generation",
    "state",
    "started_at",
    "ended_at",
    "duration_seconds",
    "fingerprint",
)
_STAGE_KEYS = (
    "stage_attempt_id",
    "parent_stage_id",
    "stage_code",
    "work_key",
    "ordinal",
    "source_revision_id",
    "unit_revision_id",
    "state",
    "started_at",
    "ended_at",
    "duration_seconds",
    "error_code",
)


def _page(after, limit):
    if (
        type(after) is not int
        or not 0 <= after < 2**63
        or type(limit) is not int
        or not 1 <= limit <= 128
    ):
        raise ValueError("Task queries require a nonnegative cursor and 1 to 128 records")


def _cursor(rows, limit):
    return rows[limit - 1][0] if len(rows) > limit else None


def _usage(db, bucket, identity):
    counters = db.connection.execute(
        f"SELECT stage_code, {', '.join(COUNTS)} FROM ukb_usage_counters "
        "WHERE bucket = ? AND identity = ? AND stage_code = ''",
        (bucket, identity),
    ).fetchall()
    return UsageAggregate.model_validate(summarize_counters(counters)) if counters else None


def _overview(db, row):
    return TaskOverview(
        task_id=row[1],
        operation=row[2],
        created_at=row[3],
        latest_execution_id=row[4],
        state=row[5],
        started_at=row[6],
        ended_at=row[7],
        duration_seconds=row[8],
        fingerprint=row[9],
        budget=TaskBudget(
            model_budget_id=row[10],
            used=row[11],
            max_calls=row[12],
            remaining=None if row[12] is None else max(0, row[12] - row[11]),
            deadline_at=row[13],
        ),
        usage=_usage(db, "task", row[1]),
    )


def _publication(row):
    status = (
        "not_recorded"
        if row[9] is None
        else "paused"
        if row[12] and row[13] == row[9]
        else "superseded"
        if row[8] in {"superseded", "retiring", "retired"}
        else "conflict"
        if row[11]
        else "pending"
        if row[10]
        else "synced"
    )
    return TaskPublication(
        publication_id=row[1],
        knowledge_revision_id=row[2],
        execution_id=row[3],
        generation=row[4],
        attempt_id=row[5],
        published_at=row[6],
        view_id=row[7],
        source_revision_id=row[14],
        unit_revision_id=row[15],
        projection_status=status,
        projection_details="retired" if row[8] in {"retiring", "retired"} else "recorded",
    )


class TaskStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def list(self, *, after=0, limit=63, cancelled=None):
        _page(after, limit)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            rows = db.connection.execute(
                f"SELECT {_OVERVIEW} {_LATEST}WHERE t.hidden = 0 AND t.sequence > ? "
                "ORDER BY t.sequence LIMIT ?",
                (after, limit + 1),
            ).fetchall()
            tasks = tuple(_overview(db, row) for row in rows[:limit])
            return TaskList(
                statistics_at=now(),
                kb_id=self.identity.kb_id,
                generation=self.identity.generation,
                tasks=tasks,
                next_cursor=_cursor(rows, limit),
            )

    def for_execution(self, execution_id, *, cancelled=None, **options):
        TypeAdapter(RecordId).validate_python(execution_id)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            row = db.connection.execute(
                "SELECT task_id FROM ukb_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
        return self.read(row[0], execution_id=execution_id, cancelled=cancelled, **options)

    def read(
        self,
        task_id,
        *,
        execution_id=None,
        limit=63,
        executions_after=0,
        stages_after=0,
        requests_after=0,
        publications_after=0,
        cancelled=None,
    ):
        TypeAdapter(RecordId).validate_python(task_id)
        if execution_id is not None:
            TypeAdapter(RecordId).validate_python(execution_id)
        for after in (executions_after, stages_after, requests_after, publications_after):
            _page(after, limit)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            row = db.connection.execute(
                f"SELECT {_OVERVIEW} {_LATEST}WHERE t.task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = _overview(db, row)
            execution_id = execution_id or task.latest_execution_id
            selected = db.connection.execute(
                f"SELECT {_EXECUTION} FROM ukb_executions WHERE execution_id = ? AND task_id = ?",
                (execution_id, task_id),
            ).fetchone()
            if selected is None:
                raise ValueError("Execution belongs to another task")
            executions = db.connection.execute(
                f"SELECT {_EXECUTION} FROM ukb_executions WHERE task_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, executions_after, limit + 1),
            ).fetchall()
            stages = db.connection.execute(
                f"SELECT sequence, {', '.join(_STAGE_KEYS)} FROM ukb_stage_attempts "
                "WHERE execution_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (execution_id, stages_after, limit + 1),
            ).fetchall()
            requests = db.connection.execute(
                "SELECT sequence, payload_json FROM ukb_usage_requests WHERE execution_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (execution_id, requests_after, limit + 1),
            ).fetchall()
            publications = db.connection.execute(
                "SELECT r.rowid, r.publication_id, r.revision_id, r.execution_id, r.generation, "
                "r.attempt_id, r.published_at, p.view_id, COALESCE(a.state, s.state), "
                "COALESCE(a.plan_id, s.plan_id), COALESCE(a.remaining_count, s.remaining_count), "
                "COALESCE(a.conflict_count, s.conflict_count), w.paused, w.active_plan_id, "
                "i.source_revision_id, i.unit_revision_id "
                "FROM ukb_publication_receipts r JOIN ukb_publications p USING(publication_id) "
                "LEFT JOIN ukb_revision_sync b ON b.revision_id = r.revision_id "
                "LEFT JOIN ukb_sync_plans s ON s.plan_id = b.plan_id "
                "LEFT JOIN ukb_projection_state w ON w.view_id = p.view_id "
                "LEFT JOIN ukb_sync_plans a ON a.plan_id = w.active_plan_id "
                "AND a.revision_id = r.revision_id "
                "LEFT JOIN ukb_revision_import i ON i.revision_id = r.revision_id "
                "WHERE r.execution_id = ? AND r.rowid > ? ORDER BY r.rowid LIMIT ?",
                (execution_id, publications_after, limit + 1),
            ).fetchall()
            cursors = [
                _cursor(rows, limit) for rows in (executions, stages, requests, publications)
            ]
            return TaskDetail(
                statistics_at=now(),
                kb_id=self.identity.kb_id,
                generation=self.identity.generation,
                task=task,
                execution_id=execution_id,
                execution=TaskExecution.model_validate(
                    dict(zip(_EXECUTION_KEYS, selected[1:], strict=True))
                ),
                execution_usage=_usage(db, "execution", execution_id),
                executions=tuple(
                    TaskExecution.model_validate(dict(zip(_EXECUTION_KEYS, row[1:], strict=True)))
                    for row in executions[:limit]
                ),
                stages=tuple(
                    TaskStage.model_validate(dict(zip(_STAGE_KEYS, row[1:], strict=True)))
                    for row in stages[:limit]
                ),
                requests=tuple(
                    UsageRequest.model_validate_json(row[1]) for row in requests[:limit]
                ),
                executions_after=cursors[0],
                publications=tuple(_publication(row) for row in publications[:limit]),
                stages_after=cursors[1],
                requests_after=cursors[2],
                publications_after=cursors[3],
                coverage={
                    "page_complete": all(cursor is None for cursor in cursors),
                    "model_usage": "recorded" if task.usage is not None else "not_recorded",
                    "non_model_stages": "partial",
                    "stage_usage": "not_included",
                    "resource_samples": "not_recorded",
                    "pricing": "not_recorded",
                    "projection_details": "retired"
                    if any(row[8] in {"retiring", "retired"} for row in publications[:limit])
                    else "recorded",
                },
            )
