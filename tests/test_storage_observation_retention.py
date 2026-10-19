"""Manual detail retention never removes task facts, costs or recovery records."""

import pytest
import test_storage_task_entrypoints as task_cases

from ultrakb.application.knowledge_tasks import capture_task_identity, read_knowledge_task
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.costs import capture_costs

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def age_task(root, identity, *, state="completed"):
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_executions SET ended_at = ?, state = ?",
            ("2020-01-01T00:00:00+00:00", state),
        )


def test_expired_observations_archive_in_bounded_batches_without_erasing_core_facts(imported_task):
    from ultrakb.application.storage_maintenance import (
        archive_task_observations,
        preview_storage_maintenance,
    )

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    age_task(root, identity)
    before = read_knowledge_task(root, task_id, limit=128)
    with storage_stage(root, identity, read_only=True) as db:
        core = db.connection.execute(
            "SELECT stage_attempt_id, state, started_at, ended_at, duration_seconds, "
            "source_revision_id, unit_revision_id, work_key "
            "FROM ukb_stage_attempts ORDER BY sequence"
        ).fetchall()
    preview = preview_storage_maintenance(root, max_items=1).observations
    assert preview.retention_days == 90
    assert preview.candidate_count > 1 and preview.protected_count == 0
    assert preview.selected_count == 1 and preview.has_more
    assert archive_task_observations(root, max_bytes=1).archived_count == 0
    with capture_costs() as costs:
        first = archive_task_observations(root, identity=identity, max_items=1)
    assert costs.finish().writer_commits == 1
    assert first.archived_count == 1 and first.archived_bytes > 0 and first.has_more
    after = read_knowledge_task(root, task_id, limit=128)
    retired = [stage for stage in after.stages if stage.observation_coverage == "retired"]
    assert len(retired) == 1 and retired[0].observation is None
    assert retired[0].observation_unavailable_reason == "retention_archived"
    assert after.coverage["storage_costs_retired"]
    from ultrakb.application.knowledge_tasks import read_knowledge_stage
    from ultrakb.desktop.knowledge_tasks import detail_text, stage_text

    assert "详细观测已归档" in detail_text(after)
    assert "详细观测已归档" in stage_text(
        read_knowledge_stage(root, task_id, retired[0].stage_attempt_id)
    )
    assert after.task == before.task and after.execution == before.execution
    assert after.requests == before.requests and after.publications == before.publications
    with storage_stage(root, identity, read_only=True) as db:
        assert (
            db.connection.execute(
                "SELECT stage_attempt_id, state, started_at, ended_at, duration_seconds, "
                "source_revision_id, unit_revision_id, work_key "
                "FROM ukb_stage_attempts ORDER BY sequence"
            ).fetchall()
            == core
        )
    remaining = archive_task_observations(root, max_items=1000)
    assert remaining.archived_count == preview.candidate_count - 1
    assert not remaining.has_more
    assert preview_storage_maintenance(root).observations.candidate_count == 0


@pytest.mark.parametrize("state", ["started", "failed", "cancelled", "interrupted"])
def test_unfinished_task_details_do_not_expire(imported_task, state):
    from ultrakb.application.storage_maintenance import (
        archive_task_observations,
        preview_storage_maintenance,
    )

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    recent = preview_storage_maintenance(root).observations
    assert recent.candidate_count == 0 and recent.protected_count > 0
    age_task(root, identity, state=state)
    before = read_knowledge_task(root, task_id, limit=128)
    preview = preview_storage_maintenance(root).observations
    assert preview.candidate_count == 0 and preview.protected_bytes > 0
    assert archive_task_observations(root).archived_count == 0
    assert read_knowledge_task(root, task_id, limit=128).stages == before.stages


def test_actual_continuation_invalidates_an_old_archive_selection(imported_task, monkeypatch):
    from contextlib import contextmanager

    from ultrakb.application.storage_maintenance import archive_task_observations
    from ultrakb.storage.database import Database
    from ultrakb.storage.execution_store import ExecutionStore

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    age_task(root, identity)
    before = read_knowledge_task(root, task_id, limit=128)
    transaction, injected = Database.transaction, False

    @contextmanager
    def resume_before_archive(database):
        nonlocal injected
        if not injected:
            injected = True
            ExecutionStore(root, identity).begin(
                "model_task",
                fingerprint=before.task.fingerprint,
                max_calls=before.task.budget.max_calls,
                deadline_at=before.task.budget.deadline_at,
                resume=before.execution_id,
            )
        with transaction(database) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", resume_before_archive)
    result = archive_task_observations(root)
    assert injected and result.archived_count == 0
    assert (
        read_knowledge_task(root, task_id, execution_id=before.execution_id, limit=128).stages
        == before.stages
    )


def test_archive_storage_failure_rolls_back_the_whole_bounded_batch(imported_task):
    import sqlite3

    from ultrakb.application.storage_maintenance import archive_task_observations

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    age_task(root, identity)
    before = read_knowledge_task(root, task_id, limit=128)
    target = [stage for stage in before.stages if stage.observation is not None][1]
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_observation_archive BEFORE UPDATE OF observation "
            "ON ukb_stage_attempts WHEN NEW.observation IS NULL AND "
            f"OLD.stage_attempt_id = '{target.stage_attempt_id}' "
            "BEGIN SELECT RAISE(ABORT, 'injected archive failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected archive"):
        archive_task_observations(root)
    assert read_knowledge_task(root, task_id, limit=128).stages == before.stages
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_observation_archive")
    assert archive_task_observations(root).archived_count > 1
    after = read_knowledge_task(root, task_id, limit=128)
    assert after.task == before.task and after.requests == before.requests
