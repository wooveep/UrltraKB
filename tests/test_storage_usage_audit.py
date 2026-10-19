"""Explicit counter maintenance reconstructs projections from actual request records."""

import pytest
import test_storage_task_entrypoints as task_cases

from ultrakb.application.knowledge_tasks import capture_task_identity, read_knowledge_task
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.execution_schema import COUNTS

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def test_audit_and_bounded_rebuild_preserve_actual_requests_and_budget(imported_task):
    from ultrakb.application.storage_maintenance import (
        audit_knowledge_usage,
        repair_knowledge_usage,
    )

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    before = read_knowledge_task(root, task_id)
    clean = audit_knowledge_usage(root, identity=identity, max_items=1)
    assert clean.status == "consistent" and clean.mismatched_counters == 0
    assert clean.request_count == 2
    with storage_stage(root, identity) as db:
        originals = db.connection.execute(
            "SELECT request_id, payload_json, storage_version, observation_version, "
            "field_versions_json FROM ukb_usage_requests ORDER BY sequence"
        ).fetchall()
        with db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_usage_counters SET input_total = input_total + 7 "
                "WHERE bucket = 'task' AND identity = ? AND stage_code = ''",
                (task_id,),
            )
            connection.execute(
                "DELETE FROM ukb_usage_counters WHERE bucket = 'execution' "
                "AND identity = ? AND stage_code = ''",
                (before.execution_id,),
            )
            connection.execute(
                f"INSERT INTO ukb_usage_counters SELECT bucket, ?, stage_code, {', '.join(COUNTS)} "
                "FROM ukb_usage_counters WHERE bucket = 'task' AND identity = ? "
                "AND stage_code = ''",
                ("f" * 32, task_id),
            )
    damaged = audit_knowledge_usage(root, identity=identity, max_items=1)
    assert damaged.status == "differences" and damaged.mismatched_counters == 3
    assert len(damaged.differences) == 1 and damaged.has_more
    for remaining in (2, 1, 0):
        repaired = repair_knowledge_usage(root, identity=identity, max_items=1)
        assert repaired.status == "repaired" and repaired.repaired_counters == 1
        assert repaired.remaining == remaining
    clean = audit_knowledge_usage(root, identity=identity)
    assert clean.status == "consistent" and clean.mismatched_counters == 0
    after = read_knowledge_task(root, task_id)
    assert after.task.usage == before.task.usage
    assert after.task.budget.used == before.task.budget.used == 2
    assert clean.statistics_version == damaged.statistics_version + 3
    with storage_stage(root, identity, read_only=True) as db:
        assert (
            db.connection.execute(
                "SELECT request_id, payload_json, storage_version, observation_version, "
                "field_versions_json FROM ukb_usage_requests ORDER BY sequence"
            ).fetchall()
            == originals
        )


def test_a_late_actual_receipt_prevents_repair_from_publishing_a_stale_snapshot(
    imported_task, monkeypatch
):
    from contextlib import contextmanager

    from ultrakb.application.storage_maintenance import (
        audit_knowledge_usage,
        repair_knowledge_usage,
    )
    from ultrakb.storage.database import Database
    from ultrakb.storage.usage_store import UsageStore

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    execution = read_knowledge_task(root, task_id).execution_id
    usage = UsageStore(root, identity)
    request = usage.requests(execution_id=execution).records[0]
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_usage_counters SET input_total = input_total + 7 "
            "WHERE bucket = 'task' AND identity = ? AND stage_code = ''",
            (task_id,),
        )
    transaction, injected = Database.transaction, False

    @contextmanager
    def receive_before_repair(database):
        nonlocal injected
        if not injected:
            injected = True
            usage.merge(
                request.request_attempt_id, version=1000, input_total=(request.input_total or 0) + 5
            )
        with transaction(database) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", receive_before_repair)
    result = repair_knowledge_usage(root, identity=identity)
    assert injected and result.status == "conflict"
    assert result.repaired_counters == 0 and result.remaining is None
    assert result.unavailable_reason == "usage_changed"
    assert usage.request(request.request_attempt_id).input_total == (request.input_total or 0) + 5
    assert repair_knowledge_usage(root, identity=identity).repaired_counters == 1
    assert audit_knowledge_usage(root, identity=identity).status == "consistent"


@pytest.mark.parametrize("damage", ["payload", "stage_code"])
def test_invalid_core_requests_are_reported_without_rebuilding_known_counters(
    imported_task, damage
):
    from ultrakb.application.storage_maintenance import repair_knowledge_usage

    root, _, _ = imported_task
    identity = capture_task_identity(root)
    with storage_stage(root, identity) as db:
        before = db.connection.execute(
            "SELECT * FROM ukb_usage_counters ORDER BY 1, 2, 3"
        ).fetchall()
        with db.transaction() as connection:
            if damage == "payload":
                connection.execute(
                    "UPDATE ukb_usage_requests SET payload_json = ? WHERE sequence = "
                    "(SELECT min(sequence) FROM ukb_usage_requests)",
                    ('{"schema_version":999}',),
                )
            else:
                connection.execute(
                    "UPDATE ukb_usage_requests SET stage_code = '' WHERE sequence = "
                    "(SELECT min(sequence) FROM ukb_usage_requests)"
                )
    result = repair_knowledge_usage(root, identity=identity)
    assert result.status == "unavailable" and result.invalid_request_count == 1
    assert result.mismatched_counters is result.remaining is None
    assert result.repaired_counters == 0 and result.unavailable_reason == "invalid_request_record"
    with storage_stage(root, identity, read_only=True) as db:
        assert (
            db.connection.execute("SELECT * FROM ukb_usage_counters ORDER BY 1, 2, 3").fetchall()
            == before
        )


def test_a_concurrent_counter_repair_also_invalidates_an_old_audit(imported_task, monkeypatch):
    from contextlib import contextmanager

    from ultrakb.application.storage_maintenance import (
        audit_knowledge_usage,
        repair_knowledge_usage,
    )
    from ultrakb.storage.database import Database

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_usage_counters SET input_total = input_total + 7 "
            "WHERE bucket = 'task' AND identity = ? AND stage_code = ''",
            (task_id,),
        )
    transaction, injected = Database.transaction, False

    @contextmanager
    def repair_before_writer(database):
        nonlocal injected
        if not injected:
            injected = True
            other = repair_knowledge_usage(root, identity=identity)
            assert other.repaired_counters == 1
        with transaction(database) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", repair_before_writer)
    result = repair_knowledge_usage(root, identity=identity)
    assert injected and result.status == "conflict" and result.repaired_counters == 0
    assert result.unavailable_reason == "usage_changed"
    assert audit_knowledge_usage(root, identity=identity).status == "consistent"


def test_unrepresentable_counter_totals_do_not_overflow_or_modify_request_records(imported_task):
    from ultrakb.application.storage_maintenance import repair_knowledge_usage
    from ultrakb.storage.usage_store import UsageStore

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    usage = UsageStore(root, identity)
    execution = read_knowledge_task(root, task_id).execution_id
    requests = usage.requests(execution_id=execution).records
    for request in requests:
        usage.merge(request.request_attempt_id, version=1000, input_total=2**62 + 10)
    with storage_stage(root, identity, read_only=True) as db:
        before = db.connection.execute(
            "SELECT * FROM ukb_usage_counters ORDER BY 1, 2, 3"
        ).fetchall()
    result = repair_knowledge_usage(root, identity=identity)
    assert result.status == "unavailable" and result.unavailable_reason == "counter_integer_limit"
    assert result.repaired_counters == 0 and result.remaining is None
    assert all(
        usage.request(record.request_attempt_id).input_total == 2**62 + 10 for record in requests
    )
    with storage_stage(root, identity, read_only=True) as db:
        assert (
            db.connection.execute("SELECT * FROM ukb_usage_counters ORDER BY 1, 2, 3").fetchall()
            == before
        )
