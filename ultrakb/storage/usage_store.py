"""Idempotent partial transport receipts and constant-work incremental counters."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ultrakb.llm_usage import UsageRequest
from ultrakb.llm_usage_models import UsageAggregate, UsageReceipt

from .coordination import storage_stage
from .database import DatabaseIdentity
from .execution_schema import COUNTS, TOKENS
from .execution_store import _label

_RECEIPT_FIELDS = {
    *TOKENS,
    "state",
    "send_status",
    "business_state",
    "provider_request_id",
    "ended_at",
    "observation",
    "application_cache_hit",
}


def _counts(record):
    return {
        **{key: getattr(record, key) or 0 for key in TOKENS},
        **{f"missing_{key}": int(getattr(record, key) is None) for key in TOKENS},
        "requests": 1,
        "actual_sends": int(record.send_status == "sent"),
        "unsent_attempts": int(record.send_status == "not_sent"),
        "send_unknown": int(record.send_status == "unknown"),
        "unknown_requests": int(record.usage_status != "known"),
        "in_flight_requests": int(record.state == "started"),
        "collection_pending": int(record.observation != "transport"),
        "usage_pending": int(record.usage_status != "known" and record.send_status != "not_sent"),
    }


def _buckets(record, task_id):
    yield "execution", record.execution_id
    yield "task", task_id
    if record.source_id:
        yield "source", record.source_id
        yield "execution_source", f"{record.execution_id}:{record.source_id}"
    if record.unit_id:
        yield "unit", record.unit_id
        yield "execution_unit", f"{record.execution_id}:{record.unit_id}"
    if record.root_import_id:
        yield "import", record.root_import_id


def _encode(record):
    encoded = record.model_dump_json()
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("Request metadata exceeds the storage item limit")
    if any(getattr(record, key) is not None and getattr(record, key) >= 2**63 for key in TOKENS):
        raise ValueError("Usage counts exceed the database integer limit")
    return encoded


def validate_receipt_fields(fields):
    if not isinstance(fields, dict) or set(fields) - _RECEIPT_FIELDS:
        raise ValueError("A receipt cannot change its original request identity")
    _encode(
        UsageRequest.model_validate(
            {
                "request_attempt_id": "0" * 32,
                "execution_id": "0" * 32,
                "model": "validation",
                "stage": "validation",
                "started_at": "",
                **fields,
            }
        )
    )


def require_request_generation(db, request_id):
    row = db.connection.execute(
        "SELECT e.generation FROM ukb_usage_requests r JOIN ukb_executions e "
        "ON e.execution_id = r.execution_id WHERE r.request_id = ?",
        (request_id,),
    ).fetchone()
    if not row:
        raise KeyError(request_id)
    if row[0] != db.identity.generation:
        raise ValueError("A receipt belongs to an execution in another generation")


def _apply_delta(connection, record, stage_code, before):
    after = _counts(record)
    delta = tuple(after[key] - before[key] for key in COUNTS)
    columns = ", ".join(COUNTS)
    update = ", ".join(f"{key} = {key} + ?" for key in COUNTS)
    placeholders = ", ".join("?" for _ in COUNTS)
    task_id = connection.execute(
        "SELECT task_id FROM ukb_executions WHERE execution_id = ?", (record.execution_id,)
    ).fetchone()[0]
    for bucket, identity in _buckets(record, task_id):
        for stage in ("", stage_code):
            # Two indexed rows per identity; no callback scans the request history.
            connection.execute(
                f"INSERT INTO ukb_usage_counters(bucket, identity, stage_code, {columns}) "
                f"VALUES (?, ?, ?, {placeholders}) ON CONFLICT DO NOTHING",
                (bucket, identity, stage, *([0] * len(COUNTS))),
            )
            connection.execute(
                f"UPDATE ukb_usage_counters SET {update} "
                "WHERE bucket = ? AND identity = ? AND stage_code = ?",
                (*delta, bucket, identity, stage),
            )


def interrupt_restored_requests(db):
    """Private restore state is independent of the provider's receipt version space."""
    after = 0
    while True:
        rows = db.connection.execute(
            "SELECT sequence, request_id, stage_code, payload_json, budget_charged, "
            "model_budget_id FROM ukb_usage_requests WHERE sequence > ? "
            "AND json_extract(payload_json, '$.state') = 'started' ORDER BY sequence LIMIT 128",
            (after,),
        ).fetchall()
        if not rows:
            return
        # Parse and encode outside the short state/counter transaction. No provider
        # field version is invented, including at the maximum legal int64 version.
        prepared = []
        for _, request_id, stage, payload, prior_charge, budget in rows:
            previous = UsageRequest.model_validate_json(payload)
            current = UsageRequest.model_validate(
                {**previous.model_dump(), "state": "interrupted", "ended_at": None}
            )
            prepared.append(
                (request_id, stage, previous, current, _encode(current), prior_charge, budget)
            )
        with db.transaction() as connection:
            for request_id, stage, previous, current, encoded, prior_charge, budget in prepared:
                charged = int(current.send_status != "not_sent")
                connection.execute(
                    "UPDATE ukb_usage_requests SET payload_json = ?, budget_charged = ?, "
                    "storage_version = storage_version + 1 WHERE request_id = ?",
                    (encoded, charged, request_id),
                )
                if charged != prior_charge:
                    connection.execute(
                        "UPDATE ukb_model_budgets SET used = used + ? WHERE model_budget_id = ?",
                        (charged - prior_charge, budget),
                    )
                _apply_delta(connection, current, stage, _counts(previous))
        after = rows[-1][0]


@dataclass(frozen=True)
class RequestPage:
    records: tuple[UsageRequest, ...]
    next_cursor: int | None


class UsageStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def receipt(
        self, *, execution_id: str, source_id: str | None = None, unit_id: str | None = None
    ) -> dict:
        from .database import database_path

        total = self.summary(execution_id=execution_id, source_id=source_id, unit_id=unit_id)
        cumulative = (
            self.summary(source_id=source_id, unit_id=unit_id)
            if (source_id is not None or unit_id is not None)
            else total
        )
        return UsageReceipt(
            kb_id=self.identity.kb_id,
            generation=self.identity.generation,
            execution_ids=[execution_id],
            source_ids=[source_id] if source_id is not None else [],
            unit_ids=[unit_id] if unit_id is not None else [],
            selections=[{"execution_id": execution_id, "source_id": source_id, "unit_id": unit_id}],
            ledger=str(database_path(self.root)),
            current=UsageAggregate.model_validate(total),
            cumulative=UsageAggregate.model_validate(cumulative),
            history_status="recorded"
            if source_id is not None or unit_id is not None
            else "execution_only",
        ).model_dump(mode="json")

    def combined_receipt(self, receipts) -> dict:
        """Combine execution/source receipts from indexed counters, never request files."""
        from .usage_selection import combine_usage

        return combine_usage(self, receipts)

    def selected_summary(self, parts, *, subtract=(), database=None):
        """Sum disjoint scope buckets, subtracting selected execution intersections."""
        if database is None:
            with storage_stage(self.root, self.identity, read_only=True) as db:
                db.connection.execute("BEGIN")
                return self.selected_summary(parts, subtract=subtract, database=db)
        totals = {}

        def merge(rows, sign=1):
            for stage, *counts in rows:
                previous = totals.setdefault(stage, [0] * len(COUNTS))
                for i, value in enumerate(counts):
                    previous[i] += sign * value

        db = database
        if db.identity != self.identity or not db.connection.in_transaction:
            raise ValueError("Usage unions require this database's active read snapshot")
        for bucket, identities in parts:
            identities = sorted(identities)
            for offset in range(0, len(identities), 63):
                batch = identities[offset : offset + 63]
                merge(
                    db.connection.execute(
                        f"SELECT stage_code, {', '.join('sum(' + key + ')' for key in COUNTS)} "
                        "FROM ukb_usage_counters WHERE bucket = ? AND identity IN "
                        f"({','.join('?' for _ in batch)}) GROUP BY stage_code",
                        (bucket, *batch),
                    ).fetchall()
                )
        for bucket, execution_ids, selected in subtract:
            for execution_id in execution_ids:
                # An indexed prefix reads only this execution's intersections,
                # rather than multiplying every execution by every source.
                rows = db.connection.execute(
                    f"SELECT identity, stage_code, {', '.join(COUNTS)} "
                    "FROM ukb_usage_counters WHERE bucket = ? "
                    "AND identity >= ? AND identity < ?",
                    (bucket, execution_id + ":", execution_id + ";"),
                ).fetchall()
                merge([row[1:] for row in rows if row[0].split(":", 1)[1] in selected], -1)
        return summarize_counters([(stage, *counts) for stage, counts in totals.items()])

    def begin(
        self,
        request: UsageRequest,
        *,
        model_budget_id: str,
        stage_code: str,
        stage_attempt_id: str | None = None,
    ):
        _label(stage_code, limit=64)
        if request.state != "started":
            raise ValueError("A send intent must be persisted before its outcome")
        encoded = _encode(request)
        intent_digest = hashlib.sha256(
            json.dumps(
                request.model_dump(exclude=_RECEIPT_FIELDS | {"usage_status"}), sort_keys=True
            ).encode()
        ).hexdigest()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            execution = connection.execute(
                "SELECT model_budget_id, generation, state FROM ukb_executions "
                "WHERE execution_id = ?",
                (request.execution_id,),
            ).fetchone()
            if not execution or execution[:2] != (model_budget_id, self.identity.generation):
                raise ValueError("The request execution belongs to another generation or budget")
            previous = connection.execute(
                "SELECT execution_id, model_budget_id, stage_code, intent_digest, stage_attempt_id "
                "FROM ukb_usage_requests "
                "WHERE request_id = ?",
                (request.request_attempt_id,),
            ).fetchone()
            if previous:
                if previous != (
                    request.execution_id,
                    model_budget_id,
                    stage_code,
                    intent_digest,
                    stage_attempt_id,
                ):
                    raise ValueError("A send identity cannot be reassigned")
                return
            if execution != (model_budget_id, self.identity.generation, "started"):
                raise ValueError("A model send requires a durable active execution and budget")
            if stage_attempt_id is not None:
                stage = connection.execute(
                    "SELECT execution_id, stage_code, state FROM ukb_stage_attempts "
                    "WHERE stage_attempt_id = ?",
                    (stage_attempt_id,),
                ).fetchone()
                if stage != (request.execution_id, stage_code, "started"):
                    raise ValueError("A request must belong to its actual active stage attempt")
            reserved = connection.execute(
                "UPDATE ukb_model_budgets SET used = used + 1 WHERE model_budget_id = ? "
                "AND (max_calls IS NULL OR used < max_calls) "
                "AND (deadline_at IS NULL OR deadline_at > unixepoch('subsec'))",
                (model_budget_id,),
            ).rowcount
            if not reserved:
                from ultrakb.llm_execution import ModelBudgetExceeded

                raise ModelBudgetExceeded("The persisted model budget or deadline is exhausted")
            connection.execute(
                "INSERT INTO ukb_usage_requests(request_id, execution_id, model_budget_id, "
                "logical_call_id, source_id, unit_id, root_import_id, stage_code, "
                "stage_attempt_id, "
                "storage_version, observation_version, intent_digest, field_versions_json, "
                "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, '{}', ?)",
                (
                    request.request_attempt_id,
                    request.execution_id,
                    model_budget_id,
                    request.logical_call_id,
                    request.source_id,
                    request.unit_id,
                    request.root_import_id,
                    stage_code,
                    stage_attempt_id,
                    intent_digest,
                    encoded,
                ),
            )
            _apply_delta(connection, request, stage_code, dict.fromkeys(COUNTS, 0))

    def merge(self, request_id: str, *, version: int, **fields) -> UsageRequest:
        if type(version) is not int or not 0 <= version < 2**63:
            raise ValueError("Receipt versions must be nonnegative integers")
        validate_receipt_fields(fields)
        with storage_stage(self.root, self.identity) as db:
            for _ in range(8):
                row = db.connection.execute(
                    "SELECT r.storage_version, r.observation_version, r.stage_code, "
                    "r.payload_json, r.field_versions_json, e.generation, "
                    "r.budget_charged, r.model_budget_id "
                    "FROM ukb_usage_requests r JOIN ukb_executions e "
                    "ON e.execution_id = r.execution_id WHERE r.request_id = ?",
                    (request_id,),
                ).fetchone()
                if not row:
                    raise KeyError(request_id)
                if row[5] != self.identity.generation:
                    raise ValueError("A receipt belongs to an execution in another generation")
                previous = UsageRequest.model_validate_json(row[3])
                values = previous.model_dump()
                versions = json.loads(row[4])
                sent = previous.send_status == "sent" or fields.get("send_status") == "sent"
                if previous.application_cache_hit and previous.send_status != "sent" and sent:
                    # These zeros describe local reuse, not the late transport's usage.
                    for key in TOKENS:
                        values[key] = None
                        versions.pop(key, None)
                for key, value in fields.items():
                    if sent and fields.get("application_cache_hit") is True and key in TOKENS:
                        continue
                    if value is not None and (
                        values[key] is None or version > versions.get(key, -1)
                    ):
                        values[key] = value
                        versions[key] = version
                if sent:
                    values["send_status"] = "sent"
                if previous.state != "started" and values["state"] == "started":
                    values["state"] = previous.state
                if previous.observation == "transport":
                    values["observation"] = "transport"
                if (
                    previous.business_state == "cancelled"
                    or fields.get("business_state") == "cancelled"
                ):
                    values["business_state"] = "cancelled"
                if previous.application_cache_hit or fields.get("application_cache_hit") is True:
                    values["application_cache_hit"] = True
                    if not sent:
                        values["send_status"] = "not_sent"
                        for key in TOKENS:
                            values[key] = 0
                known = sum(values[key] is not None for key in TOKENS)
                values["usage_status"] = (
                    "known" if known == len(TOKENS) else "partial" if known else "unknown"
                )
                current = UsageRequest.model_validate(values)
                charged = int(current.send_status != "not_sent" or current.state == "started")
                encoded = _encode(current)
                encoded_versions = json.dumps(versions, sort_keys=True)
                if encoded == row[3] and encoded_versions == row[4]:
                    return current
                before = _counts(previous)
                with db.transaction() as connection:
                    changed = connection.execute(
                        "UPDATE ukb_usage_requests SET storage_version = storage_version + 1, "
                        "observation_version = ?, payload_json = ?, field_versions_json = ?, "
                        "budget_charged = ? "
                        "WHERE request_id = ? AND storage_version = ?",
                        (
                            max(version, row[1]),
                            encoded,
                            encoded_versions,
                            charged,
                            request_id,
                            row[0],
                        ),
                    ).rowcount
                    if changed:
                        # Admission holds one call until known unsent. Late proof of
                        # a send must be counted even if its reservation was released.
                        if charged != row[6]:
                            connection.execute(
                                "UPDATE ukb_model_budgets SET used = used + ? "
                                "WHERE model_budget_id = ?",
                                (charged - row[6], row[7]),
                            )
                        _apply_delta(connection, current, row[2], before)
                if changed:
                    return current
        raise RuntimeError("Concurrent receipt updates exceeded the bounded retry limit")

    def request(self, request_id: str) -> UsageRequest:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT payload_json FROM ukb_usage_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not row:
                raise KeyError(request_id)
            return UsageRequest.model_validate_json(row[0])

    def requests(self, *, execution_id: str, after: int = 0, limit: int = 63) -> RequestPage:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Request details require a bounded cursor page")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT sequence, payload_json FROM ukb_usage_requests "
                "WHERE execution_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (execution_id, after, limit + 1),
            ).fetchall()
        page = rows[:limit]
        return RequestPage(
            tuple(UsageRequest.model_validate_json(row[1]) for row in page),
            page[-1][0] if len(rows) > limit else None,
        )

    def summary(
        self,
        *,
        execution_id: str | None = None,
        source_id: str | None = None,
        unit_id: str | None = None,
    ) -> dict:
        if execution_id is None and source_id is None and unit_id is None:
            raise ValueError("Select an execution, source or unit summary")
        bucket, identity = (
            ("execution_unit", f"{execution_id}:{unit_id}")
            if execution_id and unit_id
            else ("unit", unit_id)
            if unit_id
            else ("execution_source", f"{execution_id}:{source_id}")
            if execution_id and source_id
            else ("execution", execution_id)
            if execution_id
            else ("source", source_id)
        )
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if (
                source_id is not None
                and unit_id is not None
                and db.connection.execute(
                    "SELECT source_id FROM ukb_import_units WHERE unit_id = ?", (unit_id,)
                ).fetchone()
                != (source_id,)
            ):
                raise ValueError("The usage unit belongs to another source")
            rows = db.connection.execute(
                f"SELECT stage_code, {', '.join(COUNTS)} FROM ukb_usage_counters "
                "WHERE bucket = ? AND identity = ? ORDER BY stage_code",
                (bucket, identity),
            ).fetchall()
        return summarize_counters(rows)


def summarize_counters(rows):
    counters = {row[0]: dict(zip(COUNTS, row[1:], strict=True)) for row in rows}
    totals = counters.get("", dict.fromkeys(COUNTS, 0))
    result = {
        **{
            key: totals[key]
            for key in (
                *TOKENS,
                "requests",
                "actual_sends",
                "unsent_attempts",
                "send_unknown",
                "unknown_requests",
                "in_flight_requests",
            )
        },
        "total_known": totals["input_total"] + totals["output_total"],
        "unknown_fields": {key: totals[f"missing_{key}"] for key in TOKENS},
        "collection_complete": not totals["collection_pending"],
        "usage_complete": not totals["usage_pending"],
        "request_ids": [],
        "request_ids_complete": False,
        "stages": {
            stage: {key: counts[key] for key in ("requests", "input_total", "output_total")}
            for stage, counts in counters.items()
            if stage
        },
    }
    return UsageAggregate.model_validate(result).model_dump()
