"""Usage audit and explicit repair share one result across actual adapters."""

import json
import time
from dataclasses import asdict

import pytest
import test_storage_task_entrypoints as task_cases
from click.testing import CliRunner
from fastapi.testclient import TestClient

from ultrakb.application.knowledge_tasks import capture_task_identity
from ultrakb.application.storage_maintenance import audit_knowledge_usage
from ultrakb.storage.coordination import storage_stage

pytestmark = pytest.mark.integration
imported_task = task_cases.imported_task


def _snapshot(value, *, without_versions=False):
    result = json.loads(json.dumps(value))
    result.pop("statistics_at")
    if without_versions:
        result.pop("statistics_version")
        result.pop("applied_version")
    return result


def _damage(root, identity, task_id):
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_usage_counters SET input_total = input_total + 7 "
            "WHERE bucket = 'task' AND identity = ? AND stage_code = ''",
            (task_id,),
        )


def test_cli_and_authenticated_http_audit_and_repair_share_the_real_ledger(
    imported_task, monkeypatch
):
    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias

    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
    expected = _snapshot(asdict(audit_knowledge_usage(root)))
    runner = CliRunner()
    command = runner.invoke(cli, ["--kb-dir", str(root), "storage", "audit-usage"])
    assert command.exit_code == 0, command.output
    assert _snapshot(json.loads(command.output)) == expected
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "audit-test-token")
    register_kb_alias("audit-test", root)
    headers = {"Authorization": "Bearer audit-test-token"}
    with TestClient(create_app()) as client:
        endpoint = "/api/v1/storage"
        params = {"kb": "audit-test"}
        assert client.get(endpoint + "/usage-audit", params=params).status_code == 401
        response = client.get(endpoint + "/usage-audit", params=params, headers=headers)
        assert response.status_code == 200, response.text
        assert _snapshot(response.json()) == expected
        _damage(root, identity, task_id)
        request = {**params, "kb_id": identity.kb_id, "generation": "stale", "max_items": 1}
        stale = client.post(endpoint + "/repair-usage", json=request, headers=headers)
        assert stale.status_code == 409, stale.text
        request["generation"] = identity.generation
        repaired = client.post(endpoint + "/repair-usage", json=request, headers=headers)
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["status"] == "repaired"
        assert repaired.json()["repaired_counters"] == 1
        expected["statistics_version"] += 1
        assert _snapshot(asdict(audit_knowledge_usage(root))) == expected
    _damage(root, identity, task_id)
    command = runner.invoke(
        cli,
        ["--kb-dir", str(root), "storage", "repair-usage", "--max-items", "1"],
    )
    assert command.exit_code == 0, command.output
    cli_result = json.loads(command.output)
    assert _snapshot(cli_result, without_versions=True) == _snapshot(
        repaired.json(), without_versions=True
    )
    assert cli_result["statistics_version"] == repaired.json()["statistics_version"] + 1
    assert cli_result["applied_version"] == repaired.json()["applied_version"] + 1
    expected["statistics_version"] += 1
    assert _snapshot(asdict(audit_knowledge_usage(root))) == expected


def test_native_audit_and_explicit_repair_match_the_shared_result(imported_task):
    from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

    widgets = pytest.importorskip("PySide6.QtWidgets")
    root, task_id, _ = imported_task
    identity = capture_task_identity(root)
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
        _damage(root, identity, task_id)
        expected = _snapshot(asdict(audit_knowledge_usage(root)))
        dialog.audit_usage()
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert _snapshot(asdict(dialog.last_result)) == expected
        assert "发现差异：1" in dialog.body.toPlainText()
        dialog.repair_usage()
        until(lambda: dialog.last_result is not None and not dialog.busy)
        assert dialog.last_result.status == "repaired"
        assert dialog.last_result.repaired_counters == 1
        assert audit_knowledge_usage(root).status == "consistent"
    finally:
        dialog.reject()
        app.processEvents()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "stage_module", "method"),
    [
        ("usage-audit", "ultrakb.storage.usage_audit", "GET"),
        ("preview", "ultrakb.storage.root_retirement", "GET"),
        ("archive-observations", "ultrakb.storage.observation_retention", "POST"),
    ],
)
async def test_http_cancellation_joins_the_real_storage_reader(
    imported_task, monkeypatch, route, stage_module, method
):
    import asyncio
    import importlib
    from contextlib import contextmanager
    from threading import Event

    import httpx

    from ultrakb.api import create_app
    from ultrakb.config import register_kb_alias
    from ultrakb.storage.coordination import try_reclamation

    root, _, _ = imported_task
    audit_module = importlib.import_module(stage_module)
    identity = capture_task_identity(root)
    entered, finished = Event(), Event()
    actual_stage = audit_module.storage_stage

    @contextmanager
    def watched_reader(*args, **options):
        entered.set()
        try:
            with actual_stage(*args, **options) as db:
                yield db
        finally:
            finished.set()

    monkeypatch.setattr(audit_module, "storage_stage", watched_reader)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "audit-test-token")
    register_kb_alias("audit-test", root)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url="http://test"
    ) as client:
        with try_reclamation(root, identity) as exclusive:
            assert exclusive is not None
            options = (
                {"params": {"kb": "audit-test"}}
                if method == "GET"
                else {
                    "json": {
                        "kb": "audit-test",
                        "kb_id": identity.kb_id,
                        "generation": identity.generation,
                    }
                }
            )
            request = asyncio.create_task(
                client.request(
                    method,
                    "/api/v1/storage/" + route,
                    headers={"Authorization": "Bearer audit-test-token"},
                    **options,
                )
            )
            assert await asyncio.to_thread(entered.wait, 2)
            assert not request.done() and not finished.is_set()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            assert finished.is_set()
