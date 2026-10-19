"""Task drilldown follows retained input and stage identities, not current files."""

import hashlib

import pytest
import test_storage_task_entrypoints as task_cases

from ultrakb.application.knowledge_tasks import capture_task_identity, read_knowledge_task
from ultrakb.storage.coordination import storage_stage

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def test_real_import_stages_describe_their_retained_input(imported_task):
    root, task_id, _ = imported_task
    (root.parent / "guide.md").write_text("Changed after the import.")
    detail = read_knowledge_task(root, task_id, limit=128)
    linked = [stage for stage in detail.stages if stage.source_revision_id is not None]
    assert linked
    for stage in linked:
        original = stage.input
        assert original.source_revision_id == stage.source_revision_id
        assert original.name == "guide.md"
        assert original.original_bytes == len(b"The value is 17.")
        assert original.original_digest == hashlib.sha256(b"The value is 17.").hexdigest()
        if stage.unit_revision_id is not None:
            assert original.unit_revision_id == stage.unit_revision_id
            assert original.unit_id and original.unit_key and original.document_name


def test_stage_drilldown_has_exact_requests_and_separate_input_usage(imported_task):
    from ultrakb.application.knowledge_tasks import read_knowledge_stage

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    with storage_stage(root, identity, read_only=True) as db:
        stage_id, request_id = db.connection.execute(
            "SELECT stage_attempt_id, request_id FROM ukb_usage_requests "
            "WHERE source_id IS NOT NULL ORDER BY sequence LIMIT 1"
        ).fetchone()
    detail = read_knowledge_stage(root, task_id, stage_id, identity=identity, limit=1)
    assert detail.stage.stage_attempt_id == stage_id
    assert detail.stage.input.name == "guide.md"
    assert detail.requests[0].request_attempt_id == request_id
    assert detail.current_usage.requests == detail.cumulative_usage.requests == 2
    assert detail.usage_scope == "unit"
    assert detail.coverage["request_scope"] == "exact_stage_attempt"
    assert detail.requests_after is None
    from uuid import uuid4

    with pytest.raises(KeyError):
        read_knowledge_stage(root, uuid4().hex, stage_id, identity=identity)


def test_cli_and_http_stage_pages_and_exports_share_the_query_schema(
    imported_task, tmp_path, monkeypatch
):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient
    from test_storage_task_entrypoints import snapshot

    from ultrakb.api import create_app
    from ultrakb.application.knowledge_tasks import read_knowledge_stage
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, task_id, _ = imported_task
    task = read_knowledge_task(root, task_id, limit=128)
    stage_id = next(stage.stage_attempt_id for stage in task.stages if stage.input is not None)
    expected = read_knowledge_stage(root, task_id, stage_id, limit=1)
    runner = CliRunner()
    arguments = ["--kb-dir", str(root), "tasks", "stage", task_id, stage_id, "--limit", "1"]
    command = runner.invoke(cli, arguments)
    assert command.exit_code == 0, command.output
    assert snapshot(json.loads(command.output)) == snapshot(expected.model_dump(mode="json"))
    output = tmp_path / "stage.json"
    exported = runner.invoke(cli, [*arguments, "--output", str(output)])
    assert exported.exit_code == 0, exported.output
    assert snapshot(json.loads(output.read_text())) == snapshot(expected.model_dump(mode="json"))
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    register_kb_alias("task-stage-test", root)
    with TestClient(create_app()) as client:
        url = f"/api/v1/tasks/{task_id}/stages/{stage_id}"
        params = {"kb": "task-stage-test", "limit": 1}
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        assert snapshot(response.json()) == snapshot(expected.model_dump(mode="json"))
        export = client.get(url + "/export", params=params)
        assert export.status_code == 200
        assert "attachment" in export.headers["content-disposition"]
        assert snapshot(export.json()) == snapshot(expected.model_dump(mode="json"))


def test_qt_stage_selection_and_export_use_the_same_retained_details(
    imported_task, tmp_path, monkeypatch
):
    import json
    import time

    from test_storage_task_entrypoints import snapshot

    from ultrakb.application.knowledge_tasks import read_knowledge_stage

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import Qt

    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

    root, task_id, _ = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = KnowledgeTasksDialog(root, task_id=task_id, limit=128)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.detail is not None)
        dialog.subscription.setChecked(True)
        target_id = next(stage.stage_attempt_id for stage in dialog.detail.stages if stage.input)
        iterator = widgets.QTreeWidgetItemIterator(dialog.work)
        selected = None
        while iterator.value() is not None:
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole) == target_id:
                selected = item
                break
            iterator += 1
        assert selected is not None
        dialog.work.setCurrentItem(selected)
        until(lambda: dialog.stage_detail is not None)
        expected = read_knowledge_stage(root, task_id, target_id, limit=128)
        assert snapshot(dialog.stage_detail.model_dump(mode="json")) == snapshot(
            expected.model_dump(mode="json")
        )
        assert "guide.md" in dialog.body.toPlainText()
        output = tmp_path / "qt-stage.json"
        dialog.export_to(output)
        until(output.exists)
        assert snapshot(json.loads(output.read_text())) == snapshot(
            expected.model_dump(mode="json")
        )
    finally:
        dialog.close()
        until(dialog.io.stopped)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_page", [False, True])
async def test_http_cancellation_joins_task_readers_blocked_by_reclamation(
    imported_task, monkeypatch, stage_page
):
    import asyncio
    from contextlib import contextmanager
    from threading import Event

    from httpx import ASGITransport, AsyncClient

    import ultrakb.storage.task_store as task_store
    from ultrakb.api import create_app
    from ultrakb.config import register_kb_alias
    from ultrakb.storage.coordination import try_reclamation

    root, task_id, first_page = imported_task
    identity = capture_task_identity(root)
    register_kb_alias("cancel-task-stage", root)
    entered, finished = Event(), Event()
    original = task_store.storage_stage

    @contextmanager
    def observed(*args, **kwargs):
        entered.set()
        try:
            with original(*args, **kwargs) as db:
                yield db
        finally:
            finished.set()

    monkeypatch.setattr(task_store, "storage_stage", observed)
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    url = f"/api/v1/tasks/{task_id}"
    if stage_page:
        url += f"/stages/{first_page.stages[0].stage_attempt_id}"
    with try_reclamation(root, identity) as lease:
        assert lease is not None
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            operation = asyncio.create_task(client.get(url, params={"kb": "cancel-task-stage"}))
            assert await asyncio.to_thread(entered.wait, 3)
            assert not operation.done()
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 2)
            assert finished.is_set(), "HTTP returned before the blocked storage reader exited"


def test_unavailable_input_description_does_not_hide_stages_or_known_usage(imported_task):
    from ultrakb.application.knowledge_tasks import read_knowledge_stage
    from ultrakb.storage.blobs import blob_path

    root, task_id, _ = imported_task
    detail = read_knowledge_task(root, task_id, limit=128)
    stage = next(item for item in detail.stages if item.input is not None)
    identity = capture_task_identity(root)
    with storage_stage(root, identity, read_only=True) as db:
        digest = db.connection.execute(
            "SELECT manifest_digest FROM ukb_source_revisions WHERE revision_id = ?",
            (stage.source_revision_id,),
        ).fetchone()[0]
    manifest = blob_path(root, digest)
    held = manifest.with_suffix(".held")
    manifest.rename(held)
    try:
        page = read_knowledge_task(root, task_id, identity=identity, limit=128)
        missing = next(
            item for item in page.stages if item.stage_attempt_id == stage.stage_attempt_id
        )
        assert missing.input is None
        assert missing.input_unavailable_reason == "retained_input_unavailable"
        retained = read_knowledge_stage(root, task_id, stage.stage_attempt_id, identity=identity)
        assert retained.current_usage.requests == 2
        assert retained.cumulative_usage.requests == 2
        assert retained.coverage["input_description"] == "unavailable"
    finally:
        held.rename(manifest)


def test_qt_clearing_stage_selection_rejects_its_delayed_reader(imported_task, monkeypatch):
    import time
    from contextlib import contextmanager
    from threading import Event

    import ultrakb.storage.task_store as task_store
    from ultrakb.storage.coordination import try_reclamation

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import Qt

    from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog, detail_text

    root, task_id, _ = imported_task
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = KnowledgeTasksDialog(root, task_id=task_id, limit=128)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    entered, finished = Event(), Event()
    original = task_store.storage_stage

    @contextmanager
    def observed(*args, **kwargs):
        entered.set()
        try:
            with original(*args, **kwargs) as db:
                yield db
        finally:
            finished.set()

    try:
        until(lambda: dialog.detail is not None)
        dialog.subscription.setChecked(True)
        expected = detail_text(dialog.detail)
        iterator = widgets.QTreeWidgetItemIterator(dialog.work)
        while iterator.value().data(0, Qt.ItemDataRole.UserRole) is None:
            iterator += 1
        selected = iterator.value()
        monkeypatch.setattr(task_store, "storage_stage", observed)
        with try_reclamation(root, dialog.identity) as lease:
            assert lease is not None
            dialog.work.setCurrentItem(selected)
            until(entered.is_set)
            dialog.work.setCurrentItem(dialog.work.topLevelItem(0))
            assert dialog.stage_id is None
        until(lambda: finished.is_set() and not dialog.io._operations)
        assert dialog.stage_detail is None
        assert dialog.body.toPlainText() == expected
        assert not dialog.next_stage.isEnabled()
    finally:
        dialog.close()
        until(dialog.io.stopped)
