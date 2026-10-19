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
