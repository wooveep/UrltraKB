"""Explicit full-ledger audits and bounded, generation-checked counter repairs."""

import json
from dataclasses import asdict, dataclass, replace

from ultrakb.llm_usage import UsageRequest
from ultrakb.locks import LockCancelled

from .coordination import storage_stage
from .execution_schema import COUNTS
from .execution_store import _label, now
from .maintenance import _limits
from .usage_store import _buckets, _counts, _encode


@dataclass(frozen=True)
class CounterDifference:
    bucket: str
    identity: str
    stage_code: str
    actual: tuple[int | None, ...] | None
    expected: tuple[int, ...] | None


@dataclass(frozen=True)
class UsageAuditResult:
    schema_version: int
    statistics_at: str
    kb_id: str
    generation: str
    status: str
    statistics_version: int
    request_count: int
    counter_count: int
    expected_counter_count: int
    mismatched_counters: int | None
    differences: tuple[CounterDifference, ...]
    remaining: int | None
    has_more: bool
    repaired_counters: int = 0
    invalid_request_count: int = 0
    unavailable_reason: str | None = None
    counter_columns: tuple[str, ...] = COUNTS
    applied_version: int | None = None


def _check(cancelled):
    if cancelled is not None and cancelled():
        raise LockCancelled("Usage audit cancelled")


def _expected(connection, cancelled):
    expected, request_count, invalid = {}, 0, 0
    cursor = connection.execute(
        "SELECT r.request_id, r.stage_code, r.payload_json, r.execution_id, "
        "r.source_id, r.unit_id, r.root_import_id, e.task_id "
        "FROM ukb_usage_requests r LEFT JOIN ukb_executions e USING(execution_id) "
        "ORDER BY r.sequence"
    )
    while True:
        _check(cancelled)
        rows = cursor.fetchmany(128)
        if not rows:
            return expected, request_count, invalid
        for request_id, stage, payload, execution, source, unit, imported, task in rows:
            request_count += 1
            try:
                _label(stage, limit=64)
                record = UsageRequest.model_validate_json(payload)
                _encode(record)
                if task is None or (
                    record.request_attempt_id,
                    record.execution_id,
                    record.source_id,
                    record.unit_id,
                    record.root_import_id,
                ) != (request_id, execution, source, unit, imported):
                    raise ValueError("Request payload does not match its stored identity")
            except (ValueError, TypeError):
                invalid += 1
                continue
            counts = _counts(record)
            for bucket, identity in _buckets(record, task):
                for code in ("", stage):
                    values = expected.setdefault((bucket, identity, code), [0] * len(COUNTS))
                    for index, name in enumerate(COUNTS):
                        values[index] += counts[name]


def _differences(connection, expected, max_items, max_bytes, cancelled):
    selected, count, mismatched, size = [], 0, 0, 0

    def compare(key, actual, wanted):
        nonlocal mismatched, size
        if actual == wanted:
            return
        mismatched += 1
        difference = CounterDifference(*key, actual, wanted)
        encoded_bytes = len(json.dumps(asdict(difference)).encode()) + 256
        if len(selected) < max_items and size + encoded_bytes <= max_bytes:
            selected.append(difference)
            size += encoded_bytes

    cursor = connection.execute(
        f"SELECT bucket, identity, stage_code, {', '.join(COUNTS)} "
        "FROM ukb_usage_counters ORDER BY bucket, identity, stage_code"
    )
    while True:
        _check(cancelled)
        rows = cursor.fetchmany(128)
        if not rows:
            break
        for bucket, identity, stage, *values in rows:
            count += 1
            key = (bucket, identity, stage)
            wanted = expected.pop(key, None)
            actual = tuple(value if type(value) is int else None for value in values)
            compare(key, actual, tuple(wanted) if wanted is not None else None)
    for key, values in sorted(expected.items()):
        _check(cancelled)
        compare(key, None, tuple(values))
    return count, mismatched, tuple(selected)


class UsageAudit:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def run(self, *, repair=False, max_items=128, max_bytes=256 * 1024, cancelled=None):
        _limits(max_items, max_bytes)
        if type(repair) is not bool:
            raise ValueError("Counter repair must be explicit")
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            db.connection.execute("BEGIN")
            version = db.connection.execute(
                "SELECT revision FROM ukb_usage_state WHERE singleton = 1"
            ).fetchone()[0]
            timestamp = now()
            expected, requests, invalid = _expected(db.connection, cancelled)
            expected_count = len(expected)
            count, mismatched, differences = _differences(
                db.connection, expected, min(max_items, 128), min(max_bytes, 256 * 1024), cancelled
            )
        result = UsageAuditResult(
            1,
            timestamp,
            self.identity.kb_id,
            self.identity.generation,
            "unavailable" if invalid else "differences" if mismatched else "consistent",
            version,
            requests,
            count,
            expected_count,
            None if invalid else mismatched,
            () if invalid else differences,
            None if invalid else mismatched,
            not invalid and mismatched > len(differences),
            invalid_request_count=invalid,
            unavailable_reason="invalid_request_record" if invalid else None,
        )
        if not repair or invalid or not mismatched or not differences:
            return result
        if any(value >= 2**63 for item in differences for value in (item.expected or ())):
            return replace(
                result,
                status="unavailable",
                remaining=None,
                unavailable_reason="counter_integer_limit",
            )
        _check(cancelled)
        columns = ", ".join(COUNTS)
        placeholders = ", ".join("?" for _ in COUNTS)
        update = ", ".join(f"{name} = excluded.{name}" for name in COUNTS)
        upsert = (
            f"INSERT INTO ukb_usage_counters(bucket, identity, stage_code, {columns}) "
            f"VALUES (?, ?, ?, {placeholders}) ON CONFLICT(bucket, identity, stage_code) "
            f"DO UPDATE SET {update}"
        )
        batch = tuple(
            ((item.bucket, item.identity, item.stage_code), item.expected) for item in differences
        )
        # Ledger parsing, aggregation and batch encoding all precede this writer.
        # A normal callback increments the version in its own counter transaction.
        with storage_stage(self.root, self.identity, cancelled=cancelled) as db:
            with db.transaction() as connection:
                current = connection.execute(
                    "SELECT revision FROM ukb_usage_state WHERE singleton = 1"
                ).fetchone()[0]
                if current != version:
                    return replace(
                        result,
                        status="conflict",
                        remaining=None,
                        unavailable_reason="usage_changed",
                    )
                for key, expected in batch:
                    if expected is None:
                        connection.execute(
                            "DELETE FROM ukb_usage_counters WHERE bucket = ? "
                            "AND identity = ? AND stage_code = ?",
                            key,
                        )
                    else:
                        connection.execute(upsert, (*key, *expected))
                connection.execute(
                    "UPDATE ukb_usage_state SET revision = revision + 1 WHERE singleton = 1"
                )
        remaining = mismatched - len(differences)
        return replace(
            result,
            status="repaired",
            repaired_counters=len(differences),
            remaining=remaining,
            has_more=remaining > 0,
            applied_version=version + 1,
        )
