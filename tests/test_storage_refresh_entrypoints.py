"""Development adapters expose the same main-store refresh publication."""

import asyncio
import json
from uuid import uuid4

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input
from test_storage_refresh import compiled, responses

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.refresh import refresh_status
from ultrakb.application.removal import remove_document
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
def test_empty_refresh_adapters_publish_without_model_work(tmp_path, monkeypatch, adapter):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        scope = asyncio.run(compiled(root, identity, unit))
        removed = remove_document(root, unit.source_id, scope=scope)
        assert removed.status == "removed"
        if adapter == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
            monkeypatch.setattr(cli, "_selected_scope", lambda *args: storage_scope(root, identity))
            result = CliRunner().invoke(cli.cli, ["refresh", "run"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["status"] == "completed"
            assert payload["publication_id"] == storage_scope(root, identity).publication_id
        elif adapter == "http":
            from fastapi.testclient import TestClient

            import ultrakb.api as api
            import ultrakb.api_refresh as api_refresh
            import ultrakb.api_views as api_views

            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            monkeypatch.setattr(api_refresh, "_resolve_kb", lambda *args: root)
            monkeypatch.setattr(
                api_views, "view_scope", lambda *args: storage_scope(root, identity)
            )
            with TestClient(api.create_app()) as client:
                result = client.post("/api/v1/refresh", json={"kb": "fixture"})
                assert result.status_code == 200, result.text
                payload = result.json()
                assert payload["status"] == "completed", payload
                assert payload["publication_id"] == storage_scope(root, identity).publication_id
        else:
            import ultrakb.application.views as views
            from ultrakb.runtime.records import UnitIdentity
            from ultrakb.runtime.requests import RefreshKnowledge
            from ultrakb.runtime.worker import _execute

            monkeypatch.setattr(views, "view_scope", lambda *args: storage_scope(root, identity))
            request = RefreshKnowledge(view_id="legacy")
            worker = UnitIdentity.create(
                uuid4().hex, 0, str(root), request, generation=identity.generation
            )
            result = _execute(request, worker, ExecutionContext())
            assert result.status == "completed", result
            assert result.revision == storage_scope(root, identity).publication_id
            assert result.resources == (str(root / "wiki"),)
        current = storage_scope(root, identity)
        assert not current.knowledge.unit_revisions()
        assert not refresh_status(root, scope=current)["needs_refresh"]
        assert len(service.requests) == 2
