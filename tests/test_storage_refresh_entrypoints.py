"""Development adapters expose the same main-store refresh publication."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input
from test_storage_refresh import compiled, responses
from test_storage_source_removal import retained_publication

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.refresh import refresh_status
from ultrakb.application.removal import remove_document
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore

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
                api_views, "view_scope", lambda *args, **kwargs: storage_scope(root, identity)
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


@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
def test_empty_confirmation_adapters_recover_the_same_committed_receipt(
    tmp_path, monkeypatch, adapter
):
    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    generation = catalog.source(unit.source_id).generation
    (root / ".ultrakb/config.yaml").unlink()
    publications = []
    if adapter == "cli":
        from click.testing import CliRunner

        import ultrakb.cli as cli

        monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
        monkeypatch.setattr(cli, "_selected_scope", lambda *args: storage_scope(root, identity))
        for _ in range(2):
            result = CliRunner().invoke(
                cli.cli, ["refresh", "empty", unit.source_id, "--generation", str(generation)]
            )
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["status"] == "completed", payload
            publications.append(payload["publication_id"])
    elif adapter == "http":
        from fastapi.testclient import TestClient

        import ultrakb.api as api
        import ultrakb.api_refresh as api_refresh
        import ultrakb.api_views as api_views

        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
        monkeypatch.setattr(api_refresh, "_resolve_kb", lambda *args: root)
        monkeypatch.setattr(
            api_views, "view_scope", lambda *args, **kwargs: storage_scope(root, identity)
        )
        with TestClient(api.create_app()) as client:
            for _ in range(2):
                result = client.post(
                    "/api/v1/refresh/empty",
                    json={"kb": "fixture", "source_id": unit.source_id, "generation": generation},
                )
                assert result.status_code == 200, result.text
                payload = result.json()
                assert payload["status"] == "completed", payload
                publications.append(payload["publication_id"])
    else:
        import ultrakb.application.views as views
        from ultrakb.runtime.records import UnitIdentity
        from ultrakb.runtime.requests import ConfirmEmptySource
        from ultrakb.runtime.worker import _execute

        monkeypatch.setattr(views, "view_scope", lambda *args: storage_scope(root, identity))
        request = ConfirmEmptySource(unit.source_id, generation, view_id="legacy")
        for _ in range(2):
            worker = UnitIdentity.create(
                uuid4().hex, 0, str(root), request, generation=identity.generation
            )
            result = _execute(request, worker, ExecutionContext())
            assert result.status == "completed", result
            assert result.resources == (str(root / "wiki"),)
            publications.append(result.revision)
    assert publications[0] == publications[1] == storage_scope(root, identity).publication_id
    assert publications[0] != scope.publication_id
    assert catalog.source(unit.source_id).contribution_empty


def test_desktop_empty_review_reads_the_fixed_main_store_scope(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from ultrakb.desktop.documents import DocumentsDialog, QMessageBox
    from ultrakb.knowledge_scope import live_scope

    root, identity, unit, scope = retained_publication(tmp_path)
    loaded, submitted = [], []
    table = SimpleNamespace(
        selectionModel=lambda: SimpleNamespace(
            selectedRows=lambda: [SimpleNamespace(row=lambda: 0)]
        ),
        item=lambda *args: SimpleNamespace(data=lambda role: unit.source_id),
    )
    dialog = SimpleNamespace(
        kb=root,
        scope=live_scope(root),
        read_scope=scope,
        view_id=scope.view_id,
        table=table,
        _task=None,
        _generation=0,
        _closed=False,
        invalidate=lambda: None,
        window=SimpleNamespace(
            io=SimpleNamespace(
                submit=lambda operation, callback, **kw: loaded.append((operation, callback))
            ),
            manager=SimpleNamespace(submit=lambda kb, requests: submitted.extend(requests)),
        ),
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    DocumentsDialog.confirm_empty(dialog)
    # The asynchronous read must keep the same publication even after UI navigation.
    dialog.read_scope = live_scope(root)
    operation, callback = loaded[0]
    target = operation()
    callback(target, None)
    [request] = submitted
    assert request.source_id == unit.source_id
    assert request.generation == CatalogStore(root, identity).source(unit.source_id).generation
