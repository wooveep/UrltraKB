"""Storage maintenance adapters use one bounded shared result and identity."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from test_storage_root_retirement import _two_synchronized_publications

pytestmark = pytest.mark.integration


def _snapshot(payload):
    return {key: value for key, value in payload.items() if key != "statistics_at"}


def test_cli_and_http_maintenance_share_preview_and_guarded_retirement(tmp_path, monkeypatch):
    from ultrakb.api import create_app
    from ultrakb.application.storage_maintenance import preview_storage_maintenance
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, identity, knowledge, _, first, _ = _two_synchronized_publications(tmp_path)
    expected = _snapshot(asdict(preview_storage_maintenance(root)))
    runner = CliRunner()
    command = runner.invoke(cli, ["--kb-dir", str(root), "storage", "preview"])
    assert command.exit_code == 0, command.output
    assert _snapshot(json.loads(command.output)) == expected
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "maintenance-test-token")
    register_kb_alias("maintenance-test", root)
    with TestClient(create_app()) as client:
        endpoint = "/api/v1/storage"
        params = {"kb": "maintenance-test"}
        headers = {"Authorization": "Bearer maintenance-test-token"}
        assert client.get(endpoint + "/preview", params=params).status_code == 401
        response = client.get(endpoint + "/preview", params=params, headers=headers)
        assert response.status_code == 200, response.text
        assert _snapshot(response.json()) == expected
        request = {**params, "kb_id": identity.kb_id, "generation": "stale-generation"}
        stale = client.post(endpoint + "/retire-roots", json=request, headers=headers)
        assert stale.status_code == 409, stale.text
        assert _snapshot(asdict(preview_storage_maintenance(root))) == expected
        request["generation"] = identity.generation
        retired = client.post(endpoint + "/retire-roots", json=request, headers=headers)
        assert retired.status_code == 200, retired.text
        assert retired.json()["retired_plans"] == 1
        assert client.post(endpoint + "/collect", json=request, headers=headers).status_code == 200
    assert knowledge.bind(first.publication_id).read("index.md") == b"v1"


def test_native_maintenance_uses_shared_preview_retirement_and_backup(tmp_path):
    import time

    from ultrakb.application.storage_maintenance import preview_storage_maintenance
    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, identity, knowledge, _, first, _ = _two_synchronized_publications(tmp_path)
    expected = _snapshot(asdict(preview_storage_maintenance(root)))
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = StorageMaintenanceDialog(root)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: dialog.overview is not None and not dialog.busy)
        assert _snapshot(asdict(dialog.overview)) == expected
        assert "可回收" in dialog.body.toPlainText()
        archive = tmp_path / "qt-backup"
        dialog.backup_to(archive)
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert dialog.last_result.identity == identity
        assert (archive / "backup.json").exists()
        dialog.retire_roots()
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert dialog.last_result.retired_plans == 1
        assert dialog.overview.roots.candidate_plans == 0
        assert knowledge.bind(first.publication_id).read("index.md") == b"v1"
    finally:
        dialog.reject()
        app.processEvents()


def test_cli_and_http_backups_restore_fixed_history_in_a_new_generation(tmp_path, monkeypatch):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias
    from ultrakb.storage.database import DatabaseIdentity
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _, _, first, _ = _two_synchronized_publications(tmp_path)
    runner = CliRunner()
    archive = tmp_path / "cli-backup"
    backup = runner.invoke(
        cli, ["--kb-dir", str(root), "storage", "backup", "--output", str(archive)]
    )
    assert backup.exit_code == 0, backup.output
    restored_path = tmp_path / "cli-restored"
    restored = runner.invoke(
        cli, ["storage", "restore", str(archive), "--output", str(restored_path)]
    )
    assert restored.exit_code == 0, restored.output
    receipt = json.loads(restored.output)
    assert receipt["identity"]["generation"] != identity.generation
    assert (
        KnowledgeStore(restored_path, DatabaseIdentity(**receipt["identity"]))
        .bind(first.publication_id)
        .read("index.md")
        == b"v1"
    )
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "maintenance-test-token")
    register_kb_alias("maintenance-test", root)
    headers = {"Authorization": "Bearer maintenance-test-token"}
    with TestClient(create_app()) as client:
        request = {
            "kb": "maintenance-test",
            "kb_id": identity.kb_id,
            "generation": identity.generation,
            "destination": str(tmp_path / "http-backup"),
        }
        response = client.post("/api/v1/storage/backup", json=request, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["coverage"] == json.loads(backup.output)["coverage"]
        request = {"backup": request["destination"], "destination": str(tmp_path / "http-restored")}
        response = client.post("/api/v1/storage/restore", json=request, headers=headers)
        assert response.status_code == 200, response.text
        restored_identity = DatabaseIdentity(**response.json()["identity"])
        assert restored_identity.kb_id == identity.kb_id
        assert restored_identity.generation != identity.generation
        assert (
            KnowledgeStore(Path(request["destination"]), restored_identity)
            .bind(first.publication_id)
            .read("index.md")
            == b"v1"
        )
        assert (
            client.post("/api/v1/storage/restore", json=request, headers=headers).status_code == 400
        )


def test_native_closing_cancels_waiting_preview_without_freezing_the_event_loop(tmp_path):
    import time

    from PySide6.QtCore import QTimer

    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog
    from ultrakb.storage.coordination import try_reclamation

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, identity, *_ = _two_synchronized_publications(tmp_path)
    app = widgets.QApplication.instance() or widgets.QApplication([])
    ticks = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    try:
        with try_reclamation(root, identity) as exclusive:
            assert exclusive is not None
            dialog = StorageMaintenanceDialog(root)
            deadline = time.monotonic() + 0.15
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            assert len(ticks) >= 5 and dialog.overview is None
            dialog.reject()
            deadline = time.monotonic() + 1
            while not dialog.io.stopped() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            assert dialog.io.stopped()
    finally:
        timer.stop()


@pytest.mark.asyncio
async def test_receipt_repair_via_cli_http_and_native_preserves_the_actual_paid_response(
    tmp_path, monkeypatch
):
    import time

    from stream_transport import StreamTransport
    from test_storage_questions import question_knowledge

    from ultrakb.api import create_app
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.views import storage_scope
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias
    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_maintenance_receipt BEFORE INSERT ON ukb_response_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected response acceptance failure'); END"
        )
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport([{"content": "A retained paid answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", scope=storage_scope(root, identity), context=context
        )
        assert result.status == "blocked" and len(service.requests) == 1
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_maintenance_receipt")
        packets = list(ReceiptStore(root, identity).pending.glob("*.json"))
        assert packets
        runner = CliRunner()
        command = runner.invoke(cli, ["--kb-dir", str(root), "storage", "repair-receipts"])
        assert command.exit_code == 0, command.output
        repaired = json.loads(command.output)
        assert repaired["repaired"] == len(packets) and repaired["invalid"] == 0
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "maintenance-test-token")
        register_kb_alias("maintenance-test", root)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/storage/repair-receipts",
                json={
                    "kb": "maintenance-test",
                    "kb_id": identity.kb_id,
                    "generation": identity.generation,
                },
                headers={"Authorization": "Bearer maintenance-test-token"},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"repaired": 0, "invalid": 0, "remaining": 0}
        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageMaintenanceDialog(root)

        def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert predicate(), dialog.status.text()

        try:
            until(lambda: dialog.overview is not None and not dialog.busy)
            dialog.repair_receipts()
            until(lambda: dialog.last_result is not None and not dialog.busy)
            assert dialog.last_result.repaired == 0 and dialog.last_result.remaining == 0
        finally:
            dialog.reject()
            app.processEvents()
        [request] = (
            UsageStore(root, identity)
            .requests(execution_id=context.storage_execution.execution_id)
            .records
        )
        assert b"A retained paid answer." in ReceiptStore(root, identity).read(
            request.request_attempt_id
        )
        assert request.send_status == "sent"
        assert len(service.requests) == 1


def test_native_completed_backup_remains_visible_when_followup_preview_cannot_open_the_kb(
    tmp_path, monkeypatch
):
    import time

    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog
    from ultrakb.storage.database import database_path
    from ultrakb.storage.maintenance import Maintenance

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, *_ = _two_synchronized_publications(tmp_path)
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = StorageMaintenanceDialog(root)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    backup = Maintenance.backup_package

    def make_source_unavailable_after_actual_backup(self, destination):
        receipt = backup(self, destination)
        database_path(root).rename(root / ".ultrakb/unavailable.sqlite")
        return receipt

    try:
        until(lambda: dialog.overview is not None and not dialog.busy)
        monkeypatch.setattr(
            Maintenance, "backup_package", make_source_unavailable_after_actual_backup
        )
        archive = tmp_path / "retained-backup"
        dialog.backup_to(archive)
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert (archive / "backup.json").exists()
        assert "已完成本批" in dialog.body.toPlainText()
        assert str(archive) in dialog.body.toPlainText()
        assert "预览" in dialog.status.text()
    finally:
        dialog.reject()
        app.processEvents()


def test_native_restore_is_available_without_a_readable_current_kb(tmp_path):
    import time

    from ultrakb.application.storage_maintenance import backup_knowledge
    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog
    from ultrakb.storage.database import database_path

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, identity, *_ = _two_synchronized_publications(tmp_path)
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup_knowledge(root, archive)
    database_path(root).rename(root / ".ultrakb/unavailable.sqlite")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    dialog = StorageMaintenanceDialog(root)

    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), dialog.status.text()

    try:
        until(lambda: not dialog.busy)
        assert dialog.identity is None
        assert dialog.buttons[-1].isEnabled()
        dialog.restore_to(archive, destination)
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert dialog.last_result.identity.kb_id == identity.kb_id
        assert dialog.last_result.identity.generation != identity.generation
        assert str(destination) in dialog.body.toPlainText()
    finally:
        dialog.reject()
        app.processEvents()
