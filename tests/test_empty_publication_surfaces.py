"""The CLI, REST task and desktop projection agree on publication with omissions."""

from click.testing import CliRunner
from fastapi.testclient import TestClient

from openkb.api import create_app
from openkb.application.knowledge_bases import get_kb_list
from openkb.application.source_history import source_status
from openkb.cli import cli
from openkb.desktop.source_flow_state import flow_steps


def test_cli_empty_document_finishes_and_desktop_marks_publication_complete(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "empty.txt"
    source.write_text("")
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "add", str(source)])
    assert result.exit_code == 0, result.output
    document = get_kb_list(kb_dir)["documents"][0]
    status = source_status(kb_dir, document["source_id"])
    assert status["result"]["knowledge_compilation"] == "completed"
    assert status["result"]["coverage"]["status"] == "partial"
    steps = {row.key: row for row in flow_steps(status)}
    assert steps["publication"].state == "completed"
    assert not model_service


def test_rest_batch_registers_unreadable_document_and_imports_following_file(
    kb_dir, tmp_path, monkeypatch, model_service
):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "global-config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "test", "stream": "false", "task_id": "e" * 32},
            files=[
                ("files", ("broken.docx", b"Unreadable package.", "application/octet-stream")),
                ("files", ("valid.txt", b"The required pressure is 37 kPa.", "text/plain")),
            ],
        )
        assert response.status_code == 200, response.text
        documents = [row["document"] for row in response.json()["files"]]
        assert len(documents) == 2
        assert all(row["knowledge_compilation"] == "completed" for row in documents)
        assert documents[0]["coverage"]["status"] == "partial"
        status = client.post(
            "/api/v1/source/status", json={"kb": "test", "source_id": documents[0]["source_id"]}
        )
        assert status.status_code == 200, status.text
        assert status.json()["result"]["knowledge_compilation"] == "completed"
