"""Diagnostic archive adapters preserve actual model receipts and accepted work."""

import asyncio
import json
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from test_storage_diagnostic_retention import compiler_case

from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.storage.coordination import storage_stage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_three_adapters_share_diagnostic_policy_and_preserve_paid_work(tmp_path, monkeypatch):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    async with compiler_case(tmp_path) as case:
        root, identity, _, context, service = case
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_compile_responses SET created_at = '2020-01-01T00:00:00Z'"
            )
        before = read_knowledge_task(root, context.storage_execution.task_id, limit=128)
        runner = CliRunner()
        archived = runner.invoke(
            cli, ["--kb-dir", str(root), "storage", "archive-diagnostics", "--max-items", "1"]
        )
        assert archived.exit_code == 0, archived.output
        assert json.loads(archived.output)["archived_count"] == 1
        register_kb_alias("diagnostic-archive", root)
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "diagnostic-test")
        body = {
            "kb": "diagnostic-archive",
            "kb_id": identity.kb_id,
            "generation": identity.generation,
            "max_items": 1,
        }
        with TestClient(create_app()) as client:
            endpoint = "/api/v1/storage/archive-diagnostics"
            assert client.post(endpoint, json=body).status_code == 401
            headers = {"Authorization": "Bearer diagnostic-test"}
            assert (
                client.post(
                    endpoint, json={**body, "generation": "stale"}, headers=headers
                ).status_code
                == 409
            )
            response = client.post(endpoint, json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["archived_count"] > 0
            preview = client.get(
                "/api/v1/storage/preview", params={"kb": "diagnostic-archive"}, headers=headers
            ).json()["diagnostics"]
            assert preview["retention_days"] == 30
            assert preview["payload_target_bytes"] == 512 * 1024**2
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        widgets = pytest.importorskip("PySide6.QtWidgets")
        from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageMaintenanceDialog(root)

        async def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                await asyncio.sleep(0.01)
            assert predicate(), dialog.status.text()

        try:
            await until(lambda: dialog.overview is not None and not dialog.busy)
            assert "30 天" in dialog.body.toPlainText() and "512 MiB" in dialog.body.toPlainText()
            dialog.archive_diagnostics()
            await until(lambda: dialog.last_result is not None and not dialog.busy)
            assert dialog.last_result.status == "completed"
        finally:
            dialog.close()
            await until(dialog.io.stopped)
        after = read_knowledge_task(root, context.storage_execution.task_id, limit=128)
        assert after.task == before.task and after.requests == before.requests
        assert len(service.requests) == 3
