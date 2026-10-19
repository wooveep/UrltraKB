"""Bounded read models over authoritative execution, stage and usage records."""

from pydantic import TypeAdapter

from ultrakb.llm_usage import UsageRequest
from ultrakb.llm_usage_models import UsageAggregate
from ultrakb.source_records import RecordId
from ultrakb.stage_observations import StageObservation
from ultrakb.task_records import (
    TaskBudget,
    TaskDetail,
    TaskExecution,
    TaskList,
    TaskOverview,
    TaskPublication,
    TaskStage,
    TaskStageDetail,
)

from .coordination import storage_stage
from .execution_schema import COUNTS
from .execution_store import now
from .task_inputs import describe_stage_input, describe_stage_inputs
from .usage_store import summarize_counters

_OVERVIEW = (
    "t.sequence, t.task_id, t.operation, t.created_at, e.execution_id, e.state, "
    "e.started_at, e.ended_at, e.duration_seconds, e.fingerprint, e.model_budget_id, "
    "b.used, b.max_calls, b.deadline_at, t.hidden"
)
_LATEST = (
    "FROM ukb_tasks t JOIN ukb_executions e ON e.sequence = "
    "(SELECT max(sequence) FROM ukb_executions WHERE task_id = t.task_id) "
    "JOIN ukb_model_budgets b ON b.model_budget_id = e.model_budget_id "
)
_EXECUTION = (
    "sequence, execution_id, generation, state, started_at, ended_at, duration_seconds, "
    "fingerprint, "
    "application_task_id, application_unit_id, parent_application_task_id"
)
_EXECUTION_KEYS = (
    "execution_id",
    "generation",
    "state",
    "started_at",
    "ended_at",
    "duration_seconds",
    "fingerprint",
    "application_task_id",
    "application_unit_id",
    "parent_application_task_id",
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
    "observation",
    "observation_coverage",
)


def _stage(values):
    record = dict(zip(_STAGE_KEYS, values, strict=True))
    if record["observation_coverage"] == "retired":
        record["observation_unavailable_reason"] = "retention_archived"
    if record["observation"] is not None:
        try:
            record["observation"] = StageObservation.model_validate_json(record["observation"])
        except (ValueError, TypeError):
            record["observation"] = None
            record["observation_coverage"] = "partial"
            record["observation_unavailable_reason"] = "invalid_or_unsupported_observation"
    if record["observation_coverage"] == "pending" and record["state"] != "started":
        record["observation_coverage"] = "partial"
    return TaskStage.model_validate(record)


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
        hidden=bool(row[14]),
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

    def list(self, *, after=0, limit=63, include_hidden=False, cancelled=None):
        _page(after, limit)
        if type(include_hidden) is not bool:
            raise ValueError("Hidden task inclusion must be explicit")
        visibility = "" if include_hidden else "t.hidden = 0 AND "
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            rows = db.connection.execute(
                f"SELECT {_OVERVIEW} {_LATEST}WHERE {visibility}t.sequence > ? "
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

    def set_hidden(self, task_id, *, hidden, cancelled=None):
        TypeAdapter(RecordId).validate_python(task_id)
        if type(hidden) is not bool:
            raise ValueError("Task visibility requires an explicit boolean")
        with storage_stage(self.root, self.identity, cancelled=cancelled) as db:
            with db.transaction() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM ukb_tasks WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    is None
                ):
                    raise KeyError(task_id)
                if (
                    hidden
                    and connection.execute(
                        "SELECT 1 FROM ukb_executions WHERE task_id = ? "
                        "AND state IN ('started', 'stopping') LIMIT 1",
                        (task_id,),
                    ).fetchone()
                ):
                    raise ValueError("An active task cannot be hidden")
                connection.execute(
                    "UPDATE ukb_tasks SET hidden = ? WHERE task_id = ?", (hidden, task_id)
                )
        return self.read(task_id, limit=1, cancelled=cancelled).task

    def for_execution(self, execution_id, *, cancelled=None, **options):
        TypeAdapter(RecordId).validate_python(execution_id)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            row = db.connection.execute(
                "SELECT task_id FROM ukb_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
        return self.read(row[0], execution_id=execution_id, cancelled=cancelled, **options)

    def stage(self, task_id, stage_id, *, requests_after=0, limit=63, cancelled=None):
        TypeAdapter(RecordId).validate_python(task_id)
        TypeAdapter(RecordId).validate_python(stage_id)
        _page(requests_after, limit)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            row = db.connection.execute(
                f"SELECT s.execution_id, {', '.join('s.' + key for key in _STAGE_KEYS)} "
                "FROM ukb_stage_attempts s JOIN ukb_executions e USING(execution_id) "
                "WHERE s.stage_attempt_id = ? AND e.task_id = ?",
                (stage_id, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(stage_id)
            stage = describe_stage_input(db, _stage(row[1:]))
            requests = db.connection.execute(
                "SELECT sequence, payload_json FROM ukb_usage_requests "
                "WHERE stage_attempt_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (stage_id, requests_after, limit + 1),
            ).fetchall()
            source_id, unit_id = db.connection.execute(
                "SELECT COALESCE(s.source_id, u.source_id), r.unit_id FROM (SELECT 1) "
                "LEFT JOIN ukb_source_revisions s ON s.revision_id = ? "
                "LEFT JOIN ukb_unit_revisions r ON r.unit_revision_id = ? "
                "LEFT JOIN ukb_import_units u ON u.unit_id = r.unit_id",
                (stage.source_revision_id, stage.unit_revision_id),
            ).fetchone()
            scope, selected = (
                ("unit", unit_id)
                if unit_id is not None
                else ("source", source_id)
                if source_id is not None
                else (None, None)
            )
            current = _usage(db, f"execution_{scope}", f"{row[0]}:{selected}") if scope else None
            cumulative = _usage(db, scope, selected) if scope else None
            cursor = _cursor(requests, limit)
            return TaskStageDetail(
                statistics_at=now(),
                kb_id=self.identity.kb_id,
                generation=self.identity.generation,
                task_id=task_id,
                execution_id=row[0],
                stage=stage,
                usage_scope=scope,
                current_usage=current,
                cumulative_usage=cumulative,
                requests=tuple(
                    UsageRequest.model_validate_json(item[1]) for item in requests[:limit]
                ),
                requests_after=cursor,
                coverage={
                    "page_complete": cursor is None,
                    "request_scope": "exact_stage_attempt",
                    "input_usage": "recorded" if current or cumulative else "not_recorded",
                    "input_description": "unavailable"
                    if stage.input_unavailable_reason
                    else "recorded"
                    if stage.input is not None
                    else "not_bound",
                    "storage_costs": stage.observation_coverage,
                },
            )

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
            stage_page = describe_stage_inputs(db, (_stage(row[1:]) for row in stages[:limit]))
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
                stages=stage_page,
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
                    "storage_costs": "recorded"
                    if stage_page
                    and all(item.observation_coverage == "recorded" for item in stage_page)
                    else "partial"
                    if any(item.observation_coverage != "not_recorded" for item in stage_page)
                    else "not_recorded",
                    "resource_samples": "not_recorded",
                    "storage_costs_retired": any(
                        item.observation_coverage == "retired" for item in stage_page
                    ),
                    "pricing": "not_recorded",
                    "projection_details": "retired"
                    if any(row[8] in {"retiring", "retired"} for row in publications[:limit])
                    else "recorded",
                },
            )
