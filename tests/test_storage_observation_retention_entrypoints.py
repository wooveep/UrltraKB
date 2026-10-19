"""Manual observation archive has one schema and policy in all adapters."""

import json
import time

import pytest
import test_storage_task_entrypoints as task_cases
from click.testing import CliRunner
from fastapi.testclient import TestClient
from test_storage_observation_retention import age_task

from ultrakb.application.knowledge_tasks import capture_task_identity, read_knowledge_task

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def test_cli_http_archive_retain_core_costs_and_report_coverage(imported_task, monkeypatch):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    age_task(root, identity)
    before = read_knowledge_task(root, task_id, limit=128)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--kb-dir", str(root), "storage", "archive-observations", "--max-items", "1"]
    )
    assert result.exit_code == 0, result.output
    first = json.loads(result.output)
    assert first["archived_count"] == 1 and first["observation_coverage"] == "retired"
    register_kb_alias("observation-archive", root)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "archive-test")
    body = {
        "kb": "observation-archive",
        "kb_id": identity.kb_id,
        "generation": identity.generation,
        "max_items": 1,
    }
    with TestClient(create_app()) as client:
        endpoint = "/api/v1/storage/archive-observations"
        assert client.post(endpoint, json=body).status_code == 401
        headers = {"Authorization": "Bearer archive-test"}
        assert (
            client.post(
                endpoint, json={**body, "generation": "f" * 32}, headers=headers
            ).status_code
            == 409
        )
        result = client.post(endpoint, json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["archived_count"] == 1
        assert result.json()["observation_coverage"] == first["observation_coverage"]
        preview = client.get(
            "/api/v1/storage/preview", params={"kb": "observation-archive"}, headers=headers
        )
        assert preview.json()["observations"]["retention_days"] == 90
    after = read_knowledge_task(root, task_id, limit=128)
    assert after.task == before.task and after.requests == before.requests
    assert sum(stage.observation_coverage == "retired" for stage in after.stages) == 2


def test_qt_previews_candidates_and_archives_details_without_erasing_task_facts(
    imported_task, monkeypatch
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

    root, task_id, _ = imported_task
    age_task(root, capture_task_identity(root))
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
        assert dialog.overview.observations.candidate_count > 0
        assert "90 天" in dialog.body.toPlainText()
        dialog.archive_observations()
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert dialog.last_result.archived_count > 0
        assert "已归档阶段观测" in dialog.body.toPlainText()
        retained = read_knowledge_task(root, task_id, limit=128)
        assert retained.coverage["storage_costs_retired"]
        assert retained.task.usage.actual_sends == retained.task.budget.used == 2
    finally:
        dialog.close()
        until(dialog.io.stopped)
