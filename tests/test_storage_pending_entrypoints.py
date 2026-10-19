"""Development adapters share durable pending results and real cancellation cleanup."""

import asyncio
import json
import subprocess
import sys
from threading import Event
from uuid import uuid4

import pytest
from stream_transport import StreamTransport
from test_storage_discovery import embedded_book, responses
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.pending import (
    claim_pending_job,
    pending_status,
    process_pending,
    read_pending_job,
)
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
def test_pending_source_import_delivers_the_same_publication_and_unit_facts(
    tmp_path, monkeypatch, adapter
):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        import_document(root, source, scope=storage_scope(root, identity))
        process_pending(root, max_jobs=1, scope=storage_scope(root, identity))
        scope = storage_scope(root, identity)
        if adapter == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_selected_scope", lambda *args: scope)
            completed = CliRunner().invoke(cli.cli, ["--kb-dir", str(root), "process-pending"])
            assert completed.exit_code == 0, completed.output
            outcome = json.loads(completed.stdout)["outcomes"][0]
        elif adapter == "http":
            from fastapi.testclient import TestClient

            import ultrakb.api_pending as api_pending
            import ultrakb.api_views as views
            from ultrakb.api import create_app

            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            monkeypatch.setattr(api_pending, "resolve_kb_alias", lambda *args: root)
            monkeypatch.setattr(views, "view_scope", lambda *args: scope)
            with TestClient(create_app()) as client:
                completed = client.post(
                    "/api/v1/process-pending", json={"kb": "pending-test", "view_id": "legacy"}
                )
            assert completed.status_code == 200, completed.text
            outcome = completed.json()["outcomes"][0]
        else:
            claimed = claim_pending_job(root, scope=scope)
            script = tmp_path / "pending_worker.py"
            script.write_text("""import json, multiprocessing, sys
from pathlib import Path
from uuid import uuid4

def run(directory, job, dispatch, queue):
    from ultrakb.application.execution import ExecutionContext
    import ultrakb.application.views as views
    from ultrakb.storage.database import open_database
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import RunPendingJob
    from ultrakb.runtime.worker import _execute
    root = Path(directory)
    with open_database(root, read_only=True) as db:
        identity = db.identity
    scope = views.storage_scope(root, identity)
    views.view_scope = lambda *args: scope
    request = RunPendingJob(job, dispatch, view_id="legacy")
    worker = UnitIdentity.create(uuid4().hex, 0, str(root), request, generation=identity.generation)
    queue.put(_execute(request, worker, ExecutionContext()).summary())

if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=run, args=(*sys.argv[1:], queue))
    child.start()
    result = queue.get(timeout=30)
    child.join(timeout=5)
    assert child.exitcode == 0
    print(json.dumps(result))
""")
            completed = subprocess.run(
                [sys.executable, str(script), str(root), claimed["id"], claimed["dispatch_id"]],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert completed.returncode == 0, completed.stderr[-2000:]
            outcome = json.loads(completed.stdout.splitlines()[-1])
        assert outcome["status"] == "completed", outcome
        assert (outcome.get("revision") or outcome.get("publication_id")) == storage_scope(
            root, identity
        ).publication_id
        units = outcome.get("import_units") or outcome["result"]["units"]
        assert len(units) == 1 and units[0]["status"] == "completed"
        assert len(service.requests) == 4
        assert process_pending(root, scope=storage_scope(root, identity))["processed"] == 0
        assert not (root / ".ultrakb/catalog").exists()
        assert not (root / ".ultrakb/usage").exists()


@pytest.mark.asyncio
async def test_http_drain_cancellation_waits_for_the_real_send_to_finish(tmp_path, monkeypatch):
    import ultrakb.api_pending as api_pending
    import ultrakb.api_views as views

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    gate = Event()
    gate.set()
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY"), gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        imported = await asyncio.to_thread(
            import_document, root, source, scope=storage_scope(root, identity)
        )
        assert imported.status == "added", imported.message
        await asyncio.to_thread(
            process_pending, root, max_jobs=1, scope=storage_scope(root, identity)
        )
        scope = storage_scope(root, identity)
        monkeypatch.setattr(api_pending, "resolve_kb_alias", lambda *args: root)
        monkeypatch.setattr(views, "view_scope", lambda *args: scope)
        gate.clear()
        task = asyncio.create_task(
            api_pending.drain(api_pending.DrainRequest(kb="pending-test", view_id="legacy"))
        )
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 3, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=20)
        finally:
            gate.set()
        [child] = [
            job
            for job in pending_status(root, scope=scope)["jobs"]
            if job["kind"] == "import" and job["unit_revision_id"] is None
        ]
        result = read_pending_job(root, child["id"], scope=scope)
        assert result["status"] == "stopped", result
        assert result["result"]["model_usage"]["current"]["actual_sends"] == 1
        assert storage_scope(root, identity).publication_id == scope.publication_id
        assert len(service.requests) == 3


def test_cli_and_http_metadata_do_not_require_the_working_copy_or_model_settings(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    import ultrakb.api_pending as api_pending
    import ultrakb.api_views as views
    import ultrakb.cli as cli
    from ultrakb.api import create_app

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    revision = CatalogStore(root, identity).admit(source, source_key="parent")
    scope = storage_scope(root, identity)
    work = DiscoveryStore(root, identity).schedule(
        revision, root_import_id=uuid4().hex, view_id=scope.view_id
    )
    (root / ".ultrakb/config.yaml").unlink()
    assert not (root / "wiki").exists()
    monkeypatch.setattr(cli, "_selected_scope", lambda *args: scope)
    shown = CliRunner().invoke(cli.cli, ["--kb-dir", str(root), "pending", "list"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["jobs"][0]["id"] == work.intent_id
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    monkeypatch.setattr(api_pending, "resolve_kb_alias", lambda *args: root)
    monkeypatch.setattr(views, "view_scope", lambda *args: scope)
    with TestClient(create_app()) as client:
        shown = client.get("/api/v1/pending", params={"kb": "pending-test", "view_id": "legacy"})
        detail = client.get(
            f"/api/v1/pending/{work.intent_id}", params={"kb": "pending-test", "view_id": "legacy"}
        )
        unknown = client.get(
            f"/api/v1/pending/{uuid4().hex}", params={"kb": "pending-test", "view_id": "legacy"}
        )
        invalid = client.get("/api/v1/pending", params={"kb": "pending-test", "after": 2**63})
    assert shown.status_code == 200 and detail.status_code == 200
    assert shown.json()["jobs"][0]["id"] == detail.json()["id"] == work.intent_id
    assert unknown.status_code == 404 and invalid.status_code == 422
