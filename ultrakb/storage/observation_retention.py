"""Explicit retirement of optional observations after a task has completed and aged."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .coordination import storage_stage
from .maintenance import _limits

RETENTION_DAYS = 90
_FROM = "FROM ukb_stage_attempts s JOIN ukb_executions e ON e.execution_id = s.execution_id "
_ELIGIBLE = (
    "s.state != 'started' AND EXISTS (SELECT 1 FROM ukb_executions latest "
    "WHERE latest.sequence = (SELECT max(sequence) FROM ukb_executions "
    "WHERE task_id = e.task_id) AND latest.state = 'completed' "
    "AND julianday(latest.ended_at) < julianday(?)) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_executions active WHERE active.task_id = e.task_id "
    "AND active.state IN ('started', 'stopping'))"
)


@dataclass(frozen=True)
class ObservationRetentionPreview:
    retention_days: int
    cutoff_at: str
    candidate_count: int
    candidate_bytes: int
    protected_count: int
    protected_bytes: int
    selected_count: int
    selected_bytes: int
    has_more: bool


@dataclass(frozen=True)
class ObservationArchiveResult:
    status: str
    archived_count: int = 0
    archived_bytes: int = 0
    has_more: bool = False
    observation_coverage: str = "retired"


def _cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()


def _selected(db, cutoff, max_items, max_bytes):
    cursor = db.connection.execute(
        "SELECT s.stage_attempt_id, s.observation "
        + _FROM
        + f"WHERE s.observation IS NOT NULL AND {_ELIGIBLE} ORDER BY s.sequence LIMIT ?",
        (cutoff, max_items),
    )
    rows, spent = [], 0
    try:
        for identifier, observation in cursor:
            byte_count = len(observation.encode("utf-8"))
            cost = byte_count + len(identifier) + 256
            if spent + cost > max_bytes:
                break
            rows.append((identifier, observation, byte_count))
            spent += cost
    finally:
        cursor.close()
    return rows


def _remaining(db, cutoff):
    return (
        db.connection.execute(
            "SELECT 1 " + _FROM + f"WHERE s.observation IS NOT NULL AND {_ELIGIBLE} LIMIT 1",
            (cutoff,),
        ).fetchone()
        is not None
    )


class ObservationRetention:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def preview(self, *, max_items=128, max_bytes=256 * 1024, cancelled=None):
        _limits(max_items, max_bytes)
        max_items, max_bytes = min(max_items, 128), min(max_bytes, 256 * 1024)
        cutoff = _cutoff()
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            candidates, candidate_bytes, total, total_bytes = db.connection.execute(
                "SELECT coalesce(sum(candidate), 0), coalesce(sum(CASE WHEN candidate "
                "THEN byte_count ELSE 0 END), 0), count(*), coalesce(sum(byte_count), 0) "
                f"FROM (SELECT CASE WHEN {_ELIGIBLE} THEN 1 ELSE 0 END AS candidate, "
                "length(CAST(s.observation AS BLOB)) AS byte_count "
                + _FROM
                + "WHERE s.observation IS NOT NULL)",
                (cutoff,),
            ).fetchone()
            rows = _selected(db, cutoff, max_items, max_bytes)
            return ObservationRetentionPreview(
                RETENTION_DAYS,
                cutoff,
                candidates,
                candidate_bytes,
                total - candidates,
                total_bytes - candidate_bytes,
                len(rows),
                sum(row[2] for row in rows),
                len(rows) < candidates,
            )

    def archive(self, *, max_items=128, max_bytes=256 * 1024, cancelled=None):
        _limits(max_items, max_bytes)
        max_items, max_bytes = min(max_items, 128), min(max_bytes, 256 * 1024)
        cutoff = _cutoff()
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            rows = _selected(db, cutoff, max_items, max_bytes)
            if not rows:
                return ObservationArchiveResult("completed", has_more=_remaining(db, cutoff))
        count = byte_count = 0
        with storage_stage(self.root, self.identity, cancelled=cancelled) as db:
            with db.transaction() as connection:
                for identifier, observation, size in rows:
                    changed = connection.execute(
                        "UPDATE ukb_stage_attempts AS s SET observation = NULL, "
                        "observation_coverage = 'retired' WHERE s.stage_attempt_id = ? "
                        "AND s.observation = ? AND EXISTS (SELECT 1 FROM ukb_executions e "
                        f"WHERE e.execution_id = s.execution_id AND {_ELIGIBLE})",
                        (identifier, observation, cutoff),
                    ).rowcount
                    count += changed
                    byte_count += size if changed else 0
            return ObservationArchiveResult(
                "completed", count, byte_count, has_more=_remaining(db, cutoff)
            )
