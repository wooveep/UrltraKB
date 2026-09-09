"""Source review and continuation use the existing durable task boundary."""

from fastapi.testclient import TestClient

from openkb.api import create_app
from openkb.application.documents import import_document


def test_rest_rebuild_navigation_is_queryable_and_leaves_knowledge_unchanged(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import yaml

    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    source = tmp_path / "navigation.md"
    source.write_text("# Instructions\n\nRequired version 7.")
    imported = import_document(kb_dir, source)
    before = {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["navigation"] = {"enabled": True, "processing": config["processing"]}
    path.write_text(yaml.safe_dump(config))
    source.unlink()
    with TestClient(create_app()) as client:
        binding = {
            "kb": "test",
            "source_id": imported.source_id,
            "version_id": imported.input_version,
        }
        started = client.post(
            "/api/v1/source/rebuild-navigation",
            json={
                **binding,
                "parse_id": imported.parse_id,
                "task_id": "f" * 32,
            },
        )
        assert started.status_code == 202, started.text
        task = client.app.state.import_tasks.manager.wait(started.json()["task_id"], timeout=20)
        assert task.state == "completed" and task.processes_reaped, task
        navigation = client.post("/api/v1/source/navigation", json={**binding, "limit": 1})
        assert navigation.status_code == 200, navigation.text
        assert navigation.json()["status"] == "enhanced"
        assert len(navigation.json()["positions"]) == 1
        assert navigation.json()["next_offset"] == 1
    assert {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()} == before


def test_rest_navigation_rejects_malformed_saved_status(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from openkb.locks import atomic_write_json
    from openkb.sources import SourceStore, content_id, read_object

    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    source = tmp_path / "navigation.md"
    source.write_text("Required version 7.")
    imported = import_document(kb_dir, source)
    root = SourceStore(kb_dir).root / "navigation"
    pointer = root / "latest" / f"{imported.input_version}.json"
    record = read_object(root / f"{read_object(pointer)['navigation']}.json")
    record["status"] = []
    identity = content_id(record)
    atomic_write_json(root / f"{identity}.json", record)
    atomic_write_json(pointer, {"navigation": identity})
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/source/navigation",
            json={
                "kb": "test",
                "source_id": imported.source_id,
                "version_id": imported.input_version,
            },
        )
        assert response.status_code == 404


def test_review_and_accept_saved_proposal_without_new_model_calls(
    kb_dir, tmp_path, monkeypatch, model_service
):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    (kb_dir / "wiki/index.md").write_text("# Human index\nRetain my context.\n")
    source = tmp_path / "notes.md"
    source.write_text("Saved document details.")
    pending = import_document(kb_dir, source)
    source.unlink()
    calls = len(model_service)
    with TestClient(create_app()) as client:
        binding = {"kb": "test", "source_id": pending.source_id}
        status = client.post("/api/v1/source/status", json=binding)
        assert status.status_code == 200
        assert status.json()["result"]["reason"] == "needs_acceptance"
        review = client.post(
            "/api/v1/source/proposal", json={"kb": "test", "proposal_id": pending.resume}
        )
        assert review.status_code == 200
        assert "Retain my context" in review.json()["diffs"]["index.md"]
        started = client.post(
            "/api/v1/source/continue",
            json={
                **binding,
                "version_id": pending.input_version,
                "proposal_id": pending.resume,
                "accept_pages": review.json()["protected"],
                "task_id": "a" * 32,
            },
        )
        assert started.status_code == 202, started.text
        task = client.app.state.import_tasks.manager.wait(started.json()["task_id"], timeout=20)
        assert task.state == "completed" and task.processes_reaped
        assert task.results[0].document.knowledge_compilation == "completed"
        assert len(model_service) == calls
        assert client.get("/api/v1/tasks/" + task.id).status_code == 200
        repeated = client.post(
            "/api/v1/source/continue",
            json={
                **binding,
                "version_id": pending.input_version,
                "proposal_id": pending.resume,
                "accept_pages": review.json()["protected"],
                "task_id": task.id,
            },
        )
        assert repeated.status_code == 202, repeated.text
        assert repeated.json()["task_id"] == task.id
        conflicting = client.post(
            "/api/v1/source/reparse",
            json={**binding, "version_id": pending.input_version, "task_id": task.id},
        )
        assert conflicting.status_code == 409
        assert len(model_service) == calls


def test_upload_identity_survives_temporary_storage_without_merging_same_names(
    kb_dir, tmp_path, monkeypatch, model_service
):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "test", "stream": "false", "task_id": "d" * 32},
            files=[
                ("files", ("notes.md", b"First independent upload", "text/markdown")),
                ("files", ("notes.md", b"Second independent upload", "text/markdown")),
            ],
        )
        assert response.status_code == 200, response.text
        documents = [row["document"] for row in response.json()["files"]]
        assert len({row["source_id"] for row in documents}) == 2
        for index, document in enumerate(documents):
            status = client.post(
                "/api/v1/source/status", json={"kb": "test", "source_id": document["source_id"]}
            ).json()
            assert status["source"]["origin"] == f"upload:{'d' * 32}/{index}/notes.md"
            assert status["source"]["name"] == "notes.md"
            original = client.post(
                "/api/v1/source/original",
                json={
                    "kb": "test",
                    "source_id": document["source_id"],
                    "version_id": document["input_version"],
                },
            )
            assert original.status_code == 200
            assert (
                original.content
                == [b"First independent upload", b"Second independent upload"][index]
            )


def test_rest_recompile_reports_unfinished_proposal_with_queryable_task(
    kb_dir, tmp_path, monkeypatch, model_service
):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    original = kb_dir / "notes.md"
    original.write_text("Original source")
    import_document(kb_dir, original)
    (kb_dir / "wiki/index.md").write_text("# Keep my manual index\n")
    with TestClient(create_app()) as client:
        result = client.post(
            "/api/v1/recompile", json={"kb": "test", "doc_name": "notes.md", "task_id": "e" * 32}
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["recompiled"] == 0 and payload["unfinished_count"] == 1
        assert payload["task_id"] == "e" * 32
        assert payload["docs"][0]["document"]["reason"] == "needs_acceptance"
        task = client.get("/api/v1/tasks/" + payload["task_id"]).json()
        assert task["state"] == "partial" and task["processes_reaped"]
