"""CLI, HTTP and actual Qt I/O consume the same bounded authoritative task schema."""

import json
import shutil
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.fixture
def imported_task(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    context = ExecutionContext(storage_identity=identity)
    responses = [
        {"content": json.dumps({"description": "Guide", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, context=context, scope=storage_scope(root, identity))
        assert result.status == "added", result.message
    task_id = context.storage_execution.task_id
    expected = read_knowledge_task(root, task_id, scope=storage_scope(root, identity), limit=1)
    return root, task_id, expected


def snapshot(payload):
    payload = dict(payload)
    payload.pop("statistics_at")
    return payload


def test_cli_http_details_and_exports_preserve_the_shared_page_schema(
    imported_task, tmp_path, monkeypatch
):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, task_id, expected = imported_task
    # The mutable projection is disposable; task facts remain queryable without it.
    shutil.rmtree(root / "wiki")
    runner = CliRunner()
    arguments = ["--kb-dir", str(root), "tasks", "show", task_id, "--limit", "1"]
    command = runner.invoke(cli, arguments)
    assert command.exit_code == 0, command.output
    assert snapshot(json.loads(command.output)) == snapshot(expected.model_dump(mode="json"))
    output = tmp_path / "cli-task.json"
    exported = runner.invoke(
        cli,
        [
            "--kb-dir",
            str(root),
            "tasks",
            "export",
            task_id,
            "--limit",
            "1",
            "--output",
            str(output),
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert snapshot(json.loads(output.read_text())) == snapshot(expected.model_dump(mode="json"))
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "task-test-token")
    register_kb_alias("task-test", root)
    with TestClient(create_app()) as client:
        url = f"/api/v1/tasks/{task_id}"
        params = {"kb": "task-test", "limit": 1}
        assert client.get(url, params=params).status_code == 401
        headers = {"Authorization": "Bearer task-test-token"}
        response = client.get(url, params=params, headers=headers)
        assert response.status_code == 200, response.text
        assert snapshot(response.json()) == snapshot(expected.model_dump(mode="json"))
        exported = client.get(url + "/export", params=params, headers=headers)
        assert exported.status_code == 200, exported.text
        assert "attachment" in exported.headers["content-disposition"]
        assert snapshot(exported.json()) == snapshot(expected.model_dump(mode="json"))
        assert (
            client.get("/api/v1/tasks", params=params, headers=headers).json()["tasks"][0][
                "task_id"
            ]
            == task_id
        )
        next_page = {**params, "stages_after": expected.stages_after}
        paged = client.get(url, params=next_page, headers=headers)
        cli_page = runner.invoke(cli, [*arguments, "--stages-after", str(expected.stages_after)])
        assert cli_page.exit_code == 0, cli_page.output
        assert snapshot(paged.json()) == snapshot(json.loads(cli_page.output))
        assert paged.json()["stages"][0] != response.json()["stages"][0]
        assert client.get(url, params={**params, "limit": 0}, headers=headers).status_code == 422
        assert (
            client.get("/api/v1/tasks/" + "0" * 32, params=params, headers=headers).status_code
            == 404
        )


def test_qt_task_details_and_export_read_off_thread_with_the_same_schema(
    imported_task, tmp_path, monkeypatch
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

    root, task_id, expected = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = KnowledgeTasksDialog(root, task_id=task_id, limit=1)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.detail is not None)
        assert snapshot(dialog.detail.model_dump(mode="json")) == snapshot(
            expected.model_dump(mode="json")
        )
        assert "已知发送" in dialog.body.toPlainText()
        output = tmp_path / "qt-task.json"
        dialog.export_to(output)
        until(lambda: output.exists())
        assert snapshot(json.loads(output.read_text())) == snapshot(
            expected.model_dump(mode="json")
        )
        from ultrakb.application.knowledge_tasks import capture_task_identity
        from ultrakb.storage.coordination import try_reclamation

        dialog.subscription.setChecked(True)
        paged_export = tmp_path / "qt-displayed-page.json"
        with try_reclamation(root, capture_task_identity(root)) as lease:
            assert lease is not None
            dialog.next_page("stages")
            dialog.export_to(paged_export)
        until(paged_export.exists)
        assert snapshot(json.loads(paged_export.read_text())) == snapshot(
            expected.model_dump(mode="json")
        )
    finally:
        dialog.close()
        until(dialog.io.stopped)


def test_qt_can_close_while_a_real_reclamation_lease_blocks_task_reading(
    imported_task, monkeypatch
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QTimer

    from ultrakb.application.knowledge_tasks import capture_task_identity
    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog
    from ultrakb.storage.coordination import try_reclamation

    root, task_id, _ = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    identity = capture_task_identity(root)
    ticked = []
    with try_reclamation(root, identity) as lease:
        assert lease is not None
        dialog = KnowledgeTasksDialog(root, task_id=task_id)
        QTimer.singleShot(20, lambda: ticked.append(True))
        try:
            deadline = time.monotonic() + 5
            while not ticked and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert ticked, "Qt event delivery was blocked by a task query"
            assert dialog.detail is None
        finally:
            dialog.close()
            deadline = time.monotonic() + 5
            while not dialog.io.stopped() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert dialog.io.stopped(), "Closing must cancel the storage-stage lock wait"


def test_task_adapters_read_an_unpublished_main_store_without_config_or_worktree(
    tmp_path, monkeypatch
):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore

    root = tmp_path / "unpublished"
    identity = create_database(root)
    execution = ExecutionStore(root, identity).begin(
        operation="import", fingerprint="test", max_calls=0
    )
    ExecutionStore(root, identity).finish(execution.execution_id, state="failed")
    register_kb_alias("unpublished-task", root)
    command = CliRunner().invoke(cli, ["--kb-dir", str(root), "tasks", "list"])
    assert command.exit_code == 0, command.output
    assert json.loads(command.output)["tasks"][0]["task_id"] == execution.task_id
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "task-test-token")
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/v1/tasks/" + execution.task_id,
            params={"kb": "unpublished-task"},
            headers={"Authorization": "Bearer task-test-token"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["execution_id"] == execution.execution_id
    nested = root / "legacy"
    (nested / ".ultrakb").mkdir(parents=True)
    (nested / "wiki").mkdir()
    (nested / ".ultrakb/config.yaml").write_text("model: test\n")
    monkeypatch.chdir(nested)
    command = CliRunner().invoke(cli, ["tasks", "list"])
    assert command.exit_code != 0, "Task discovery must not skip this KB and read its parent"


def test_qt_preserves_a_selected_execution_outside_the_page_and_exports_the_displayed_one(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

    root, identity, _ = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        pass
    first = context.storage_execution
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = KnowledgeTasksDialog(root, task_id=first.task_id, limit=1)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.detail is not None)
        dialog.subscription.setChecked(True)
        with context.begin(root):
            pass
        assert context.storage_execution.execution_id != first.execution_id
        output = tmp_path / "displayed-task.json"
        dialog.export_to(output)
        until(output.exists)
        assert json.loads(output.read_text())["execution_id"] == first.execution_id
        dialog.execution_id = first.execution_id
        dialog.cursors["executions_after"] = 2**63 - 1
        dialog.refresh()
        until(lambda: not dialog.detail.executions)
        assert dialog.executions.currentData() == first.execution_id
    finally:
        dialog.close()
        until(dialog.io.stopped)


def test_qt_does_not_substitute_an_unrelated_task_for_an_unlinked_queued_task(
    imported_task, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QTimer

    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog
    from ultrakb.desktop.task_details import show_task_details

    root, _, _ = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    observed = []

    def inspect():
        for window in app.topLevelWidgets():
            if isinstance(window, KnowledgeTasksDialog) and window.isVisible():
                if window.tasks.count():
                    observed.append((window.detail, window.body.toPlainText()))
                    window.accept()
                    return
        QTimer.singleShot(10, inspect)

    QTimer.singleShot(10, inspect)
    QTimer.singleShot(5000, lambda: [w.close() for w in app.topLevelWidgets()])
    show_task_details(SimpleNamespace(kb_dir=str(root), model_usage=None), None)
    assert observed
    assert observed[0][0] is None
    assert "尚未关联" in observed[0][1]
