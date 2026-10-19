"""Shared task pages expose durable facts with bounded cursors and portable JSON."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


def test_public_task_pages_and_export_follow_actual_import_attempts_without_private_payloads(
    tmp_path,
):
    from ultrakb.application.knowledge_tasks import (
        export_knowledge_task,
        list_knowledge_tasks,
        read_knowledge_task,
    )

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport([{"content": summary}, {"content": plan}]) as service:
        (root / ".env").write_text(
            f"LLM_API_KEY=task-query-secret\nOPENAI_API_BASE={service.url}\n"
        )
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
        assert result.status == "added", result.message
    task_id = context.storage_execution.task_id
    scope = storage_scope(root, identity)
    tasks = list_knowledge_tasks(root, scope=scope, limit=1)
    assert tasks.schema_version == 1
    assert tasks.statistics_at
    assert tasks.kb_id == identity.kb_id
    [task] = tasks.tasks
    assert task.task_id == task_id
    assert task.state == "completed"
    assert task.budget.used == 2
    assert task.usage.actual_sends == 2
    assert task.usage.input_total == 240
    assert task.usage.output_total == 14
    detail = read_knowledge_task(root, task_id, scope=scope, limit=1)
    assert detail.task == task
    assert detail.execution_usage.actual_sends == 2
    assert len(detail.executions) == len(detail.stages) == len(detail.requests) == 1
    assert detail.requests_after is not None
    following = read_knowledge_task(
        root, task_id, scope=scope, requests_after=detail.requests_after, limit=1
    )
    assert following.requests[0].request_attempt_id != detail.requests[0].request_attempt_id
    assert following.requests_after is None
    complete = read_knowledge_task(root, task_id, scope=scope)
    codes = {stage.stage_code for stage in complete.stages}
    assert {
        "import.admit.v1",
        "import.process.v1",
        "compile.inputs.v1",
        "compile.validate.v1",
        "compile.persist.v1",
        "knowledge.publish.v1",
        "wiki.sync.v1",
        "execution.cleanup.v1",
    } <= codes
    assert all(stage.state != "started" for stage in complete.stages)
    [generation] = [s for s in complete.stages if s.stage_code == "compile.generate.v1"]
    assert all(
        stage.parent_stage_id == generation.stage_attempt_id
        for stage in complete.stages
        if stage.stage_code == "model_request"
    )
    export = tmp_path / "task.json"
    exported = export_knowledge_task(root, task_id, export, scope=scope, limit=1)
    saved = json.loads(export.read_text())
    assert saved == exported.model_dump(mode="json")
    assert saved["requests_after"] == detail.requests_after
    assert "task-query-secret" not in export.read_text()
    assert "The value is 17." not in export.read_text()
    assert saved["coverage"]["page_complete"] is False
    assert tasks.next_cursor is None
    from ultrakb.storage.database import create_database, database_path

    other_root = tmp_path / "other-kb"
    create_database(other_root)
    target = database_path(other_root)
    header = target.read_bytes()[:16]
    with pytest.raises(ValueError, match="knowledge base"):
        export_knowledge_task(root, task_id, target, scope=scope)
    assert target.read_bytes()[:16] == header == b"SQLite format 3\x00"

    first_execution_id = context.storage_execution.execution_id
    repeated = import_document(root, source, context=context, scope=storage_scope(root, identity))
    assert repeated.status == "skipped"
    latest = read_knowledge_task(root, task_id, scope=scope, limit=1)
    assert latest.task.latest_execution_id != first_execution_id
    assert latest.task.usage.actual_sends == 2
    assert latest.task.budget.used == 2
    assert latest.executions_after is not None
    assert latest.requests == ()
    prior = read_knowledge_task(root, task_id, scope=scope, execution_id=first_execution_id)
    assert len(prior.requests) == 2
    assert prior.task.usage.actual_sends == 2

    other = import_document(root, source, scope=storage_scope(root, identity))
    assert other.status == "skipped"
    first = list_knowledge_tasks(root, scope=scope, limit=1)
    assert first.next_cursor is not None
    second = list_knowledge_tasks(root, scope=scope, limit=1, after=first.next_cursor)
    assert second.next_cursor is None
    assert second.tasks[0].task_id != first.tasks[0].task_id
    with pytest.raises(ValueError, match="another task"):
        read_knowledge_task(
            root, task_id, scope=scope, execution_id=second.tasks[0].latest_execution_id
        )


def test_task_queries_reject_unbounded_pages_and_unknown_identities(tmp_path):
    from ultrakb.application.knowledge_tasks import list_knowledge_tasks, read_knowledge_task

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    for limit in (0, 129, True):
        with pytest.raises(ValueError):
            list_knowledge_tasks(root, scope=scope, limit=limit)
    with pytest.raises(KeyError):
        read_knowledge_task(root, "0" * 32, scope=scope)


def test_task_keeps_the_committed_publication_visible_after_stage_confirmation_failure(tmp_path):
    from ultrakb.application.knowledge_tasks import read_knowledge_task
    from ultrakb.application.pages import read_page
    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_publication_observation BEFORE UPDATE ON ukb_stage_attempts "
            "WHEN OLD.stage_code = 'knowledge.publish.v1' "
            "BEGIN SELECT RAISE(ABORT, 'publication observation interrupted'); END"
        )
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    context = ExecutionContext(storage_identity=identity)
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    with StreamTransport([{"content": summary}, {"content": plan}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
        assert result.status == "added", result.message
    [unit] = result.units
    assert unit.status == "completed"
    assert read_page(
        root, f"summaries/{unit.doc_name}.md", scope=storage_scope(root, identity)
    ).body
    detail = read_knowledge_task(
        root, context.storage_execution.task_id, scope=storage_scope(root, identity)
    )
    assert detail.task.state == "completed"
    [published] = detail.publications
    assert published.knowledge_revision_id == unit.knowledge_revision_id
    assert published.execution_id == context.storage_execution.execution_id
    assert published.projection_status == "pending"
    assert detail.publications_after is None


def test_failed_cleanup_progress_subscription_still_closes_the_actual_controller(tmp_path):
    from ultrakb.llm_usage import active_scope

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    controllers = []

    def observe(event):
        active = active_scope()
        if active is not None and active.storage is not None:
            controllers.append(active.storage)
        data = event.get("data", {})
        if data.get("stage") == "execution.cleanup.v1" and data.get("status") == "started":
            raise OSError("The progress subscriber has gone away")

    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    context = ExecutionContext(storage_identity=identity, on_event=observe)
    with StreamTransport([{"content": summary}, {"content": plan}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
        assert result.status == "added", result.message
        assert "confirm execution cleanup" in result.unfinished
        assert len(service.requests) == 2
    assert controllers and all(controller._closed for controller in controllers)
