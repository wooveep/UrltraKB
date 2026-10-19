"""Task list cleanup preserves authoritative costs and recovery identities."""

import pytest
import test_storage_task_entrypoints as task_cases

from ultrakb.application.knowledge_tasks import (
    capture_task_identity,
    list_knowledge_tasks,
    read_knowledge_task,
)
from ultrakb.storage.execution_store import ExecutionStore

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def test_hidden_tasks_keep_paid_records_and_reappear_on_actual_continuation(imported_task):
    from ultrakb.application.knowledge_tasks import set_knowledge_task_visibility

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    before = read_knowledge_task(root, task_id)
    hidden = set_knowledge_task_visibility(root, task_id, hidden=True, identity=identity)
    assert hidden.hidden and not list_knowledge_tasks(root).tasks
    assert list_knowledge_tasks(root, include_hidden=True).tasks == (hidden,)
    retained = read_knowledge_task(root, task_id)
    assert retained.task.budget == before.task.budget
    assert retained.task.usage == before.task.usage
    assert retained.requests == before.requests and retained.publications == before.publications
    execution = ExecutionStore(root, identity).begin(
        "model_task",
        fingerprint=before.task.fingerprint,
        max_calls=before.task.budget.max_calls,
        deadline_at=before.task.budget.deadline_at,
        resume=before.execution_id,
    )
    assert list_knowledge_tasks(root).tasks[0].task_id == task_id
    assert not read_knowledge_task(root, task_id).task.hidden
    with pytest.raises(ValueError, match="active"):
        set_knowledge_task_visibility(root, task_id, hidden=True, identity=identity)
    ExecutionStore(root, identity).finish(execution.execution_id, state="cancelled")
    set_knowledge_task_visibility(root, task_id, hidden=True, identity=identity)
    restored = set_knowledge_task_visibility(root, task_id, hidden=False, identity=identity)
    assert not restored.hidden and restored.budget == before.task.budget
    assert restored.usage == before.task.usage


def test_visibility_rejects_another_generation_without_changing_the_task(imported_task):
    from dataclasses import replace

    from ultrakb.application.knowledge_tasks import set_knowledge_task_visibility

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    with pytest.raises(ValueError):
        set_knowledge_task_visibility(
            root, task_id, hidden=True, identity=replace(identity, generation="f" * 32)
        )
    assert not read_knowledge_task(root, task_id).task.hidden


def test_cli_and_http_visibility_preserve_shared_results(imported_task, monkeypatch):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    runner = CliRunner()
    command = runner.invoke(cli, ["--kb-dir", str(root), "tasks", "hide", task_id])
    assert command.exit_code == 0, command.output
    hidden = json.loads(command.output)
    assert hidden["hidden"] and hidden["usage"]["actual_sends"] == 2
    page = runner.invoke(cli, ["--kb-dir", str(root), "tasks", "list", "--include-hidden"])
    assert page.exit_code == 0, page.output
    assert json.loads(page.output)["tasks"] == [hidden]
    register_kb_alias("task-visibility", root)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "visibility-test")
    body = {"kb_id": identity.kb_id, "generation": identity.generation, "hidden": False}
    with TestClient(create_app()) as client:
        url = f"/api/v1/tasks/{task_id}/visibility?kb=task-visibility"
        assert client.post(url, json=body).status_code == 401
        headers = {"Authorization": "Bearer visibility-test"}
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {**hidden, "hidden": False}
        assert (
            client.post(url, json={**body, "hidden": "false"}, headers=headers).status_code == 422
        )
        assert (
            client.post(
                url, json={**body, "generation": "f" * 32, "hidden": True}, headers=headers
            ).status_code
            == 409
        )
        page = client.get(
            "/api/v1/tasks",
            params={"kb": "task-visibility", "include_hidden": True},
            headers=headers,
        )
        assert page.json()["tasks"] == [response.json()]
    assert not read_knowledge_task(root, task_id).task.hidden


def test_qt_visibility_uses_the_shared_store_off_thread(imported_task, monkeypatch):
    import time

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

    root, task_id, _ = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = KnowledgeTasksDialog(root, task_id=task_id)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.detail is not None)
        dialog.subscription.setChecked(True)
        dialog.visibility.click()
        until(lambda: dialog.detail.task.hidden)
        assert not list_knowledge_tasks(root).tasks
        dialog.show_hidden.setChecked(True)
        until(lambda: not dialog.io._operations)
        assert dialog.tasks.count() == 1
        dialog.visibility.click()
        until(lambda: not dialog.detail.task.hidden)
        assert read_knowledge_task(root, task_id).task.usage.actual_sends == 2
    finally:
        dialog.close()
        until(dialog.io.stopped)
