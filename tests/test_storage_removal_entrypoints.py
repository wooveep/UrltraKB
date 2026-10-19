"""Development bindings expose the same source withdrawal publication in all adapters."""

import json
from uuid import uuid4

import pytest
from test_storage_batch_removal import two_sources
from test_storage_source_removal import retained_publication

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.removal import preview_removal
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("adapter", ["cli", "http-json", "http-stream", "native"])
def test_source_withdrawal_adapters_preserve_publication_and_projection_result(
    tmp_path, monkeypatch, adapter
):
    root, identity, unit, scope = retained_publication(tmp_path)
    if adapter == "cli":
        from click.testing import CliRunner

        import ultrakb.cli as cli_module

        monkeypatch.setattr(cli_module, "_find_kb_dir", lambda *args: root)
        monkeypatch.setattr(
            cli_module, "_selected_scope", lambda *args, **kwargs: storage_scope(root, identity)
        )
        result = CliRunner().invoke(cli_module.cli, ["remove", unit.source_id, "--yes"])
        assert result.exit_code == 0, result.output
        assert "Historical evidence retained" in result.output
    elif adapter.startswith("http"):
        from fastapi.testclient import TestClient

        import ultrakb.api as api
        import ultrakb.api_views as api_views

        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
        monkeypatch.setattr(api, "_resolve_kb", lambda *args: root)
        monkeypatch.setattr(
            api_views, "view_scope", lambda *args, **kwargs: storage_scope(root, identity)
        )
        with TestClient(api.create_app()) as client:
            reviewed = client.post(
                "/api/v1/remove",
                json={
                    "kb": "fixture",
                    "identifier": unit.source_id,
                    "view_id": "legacy",
                    "dry_run": True,
                },
            ).json()
            assert reviewed["status"] == "dry_run" and reviewed["version"]
            request = {
                "kb": "fixture",
                "identifier": unit.source_id,
                "view_id": "legacy",
                "stream": adapter == "http-stream",
                "version": reviewed["version"],
            }
            response = client.post(
                "/api/v1/remove",
                json=request,
            )
            replayed = client.post("/api/v1/remove", json={**request, "stream": False})
            assert replayed.json()["status"] == "removed", replayed.text
            assert (
                replayed.json()["publication_id"]
                == KnowledgeStore(root, identity).bind().publication_id
            )
        assert response.status_code == 200, response.text
        if adapter == "http-stream":
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(event for event in events if event.get("status") == "removed")
        else:
            payload = response.json()
        assert payload["status"] == "removed"
        assert payload["publication_id"] == KnowledgeStore(root, identity).bind().publication_id
        assert not payload["projection_pending"]
    else:
        import ultrakb.application.views as views
        from ultrakb.runtime.records import UnitIdentity
        from ultrakb.runtime.requests import RemoveDocument
        from ultrakb.runtime.worker import _execute

        monkeypatch.setattr(
            views, "view_scope", lambda *args, **kwargs: storage_scope(root, identity)
        )
        preview = preview_removal(root, unit.source_id, scope=scope)
        request = RemoveDocument(unit.source_id, preview.version, view_id="legacy")
        worker = UnitIdentity.create(
            uuid4().hex, 0, str(root), request, generation=identity.generation
        )
        result = _execute(request, worker, ExecutionContext())
        assert result.status == "completed", result
        assert result.revision == KnowledgeStore(root, identity).bind().publication_id
        replayed = _execute(request, worker, ExecutionContext())
        assert replayed.status == "completed" and replayed.revision == result.revision
    assert CatalogStore(root, identity).source(unit.source_id).removed
    assert KnowledgeStore(root, identity).bind().read("summaries/fixture.md") == b"The value is 17."


def test_native_reviewed_batch_uses_its_database_and_publication_identity(tmp_path):
    from ultrakb.application.batch_removal import preview_batch_removal
    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import RemoveDocuments
    from ultrakb.runtime.worker import _execute

    root, identity, units = two_sources(tmp_path)
    scope = storage_scope(root, identity)
    preview = preview_batch_removal(
        root, [DocumentIdentity("source", unit.source_id) for unit in units], scope=scope
    )
    request = RemoveDocuments(preview.encode())
    worker = UnitIdentity.create(uuid4().hex, 0, str(root), request, generation=identity.generation)
    result = _execute(request, worker, ExecutionContext())
    assert result.status == "completed", result
    assert len(result.removal_items) == 2
    assert result.revision == KnowledgeStore(root, identity).bind().publication_id
    assert all(CatalogStore(root, identity).source(unit.source_id).removed for unit in units)
