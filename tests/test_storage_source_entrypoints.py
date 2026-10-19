"""Explicit retained-input reading shares one protocol across public entry points."""

import json

from click.testing import CliRunner
from fastapi.testclient import TestClient
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge
from test_storage_retained_inputs import png_bytes


def test_unpublished_source_is_read_only_by_its_exact_retained_unit(tmp_path, monkeypatch):
    from ultrakb.api import create_app
    from ultrakb.application.documents import import_document
    from ultrakb.application.views import storage_scope
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias
    from ultrakb.documents import read_document_source, read_document_view

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "retained.md"
    (tmp_path / "figure.png").write_bytes(png_bytes())
    source.write_text("![Figure](figure.png)\n")
    scope = storage_scope(root, identity)
    with StreamTransport([{"content": "Unexpected model request"}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, scope=scope)
        assert result.status == "blocked" and not service.requests
        target = result.units[0].target_revision_id
        source.unlink()
        (tmp_path / "figure.png").unlink()
        assert read_document_source(root, result.source_id, scope=scope) is None
        retained = read_document_source(
            root, result.source_id, unit_revision_id=target, scope=scope
        )
        assert retained["publication_id"] is None
        assert retained["unit_revision_id"] == target
        display = read_document_view(root, result.source_id, unit_revision_id=target, scope=scope)
        assert display.assets.images
        assert next(iter(display.assets.images.values())) == png_bytes()
        command = CliRunner().invoke(
            cli,
            [
                "--kb-dir",
                str(root),
                "source",
                result.source_id,
                "--unit-revision",
                target,
            ],
        )
        assert command.exit_code == 0, command.output
        assert json.loads(command.output)["unit_revision_id"] == target
        register_kb_alias("retained-native", root)
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "offline-native-token")
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/document/source",
                json={
                    "kb": "retained-native",
                    "hash": result.source_id,
                    "unit_revision_id": target,
                },
                headers={"Authorization": "Bearer offline-native-token"},
            )
            assert response.status_code == 200, response.text
            image = client.get(
                "/api/v1/document/source-image",
                params={
                    "kb": "retained-native",
                    "source_id": result.source_id,
                    "unit_revision_id": target,
                    "path": response.json()["images"][0]["path"],
                },
                headers={"Authorization": "Bearer offline-native-token"},
            )
            assert image.status_code == 200 and image.content == png_bytes()
            assert image.headers["X-UltraKB-Unit-Revision-ID"] == target
        assert response.status_code == 200, response.text
        assert response.json()["unit_revision_id"] == target
        assert response.json()["publication_id"] is None
