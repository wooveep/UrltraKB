"""Generator adapters retain accepted facts and model receipts through failure."""

import json
import multiprocessing

import pytest
from fastapi.testclient import TestClient
from stream_transport import StreamTransport
from test_deck_validator import GOOD_DECK
from test_storage_deck_generation import deck_knowledge, tool_response

from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.wiki_projection import ProjectionStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("entry", ["http", "sse", "cli", "native"])
@pytest.mark.parametrize("scenario", ["pending", "publication"])
def test_generator_returns_accepted_facts_through_projection_and_publication_failure(
    tmp_path, monkeypatch, entry, scenario
):
    import ultrakb.api as api
    import ultrakb.api_views as views

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    target, note = "output/decks/value/index.html", "wiki/explorations/value.md"

    def unavailable(*args, **kwargs):
        raise OSError("Projection unavailable")

    if scenario == "pending":
        monkeypatch.setattr(ArtifactStore, "sync", unavailable)
        monkeypatch.setattr(ProjectionStore, "sync", unavailable)
    else:
        from ultrakb.storage.coordination import storage_stage

        with storage_stage(root, identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_note_publish BEFORE INSERT ON ukb_publications "
                "BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END"
            )
    monkeypatch.setattr(api, "_resolve_kb", lambda *args: root)
    monkeypatch.setattr(views, "view_scope", lambda *args: scope)
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    responses = [
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "deck"),
        tool_response("write_file", {"path": note, "content": "# Retained value\n\n17"}, "note"),
        {"content": "Delivered."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        if entry == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
            monkeypatch.setattr(cli, "_selected_scope", lambda *args: scope)
            result = CliRunner().invoke(
                cli.cli, ["deck", "new", "value", "Present value.", "--json"]
            )
            assert result.exit_code == (0 if scenario == "pending" else 1), result.output
            receipt = json.loads(result.output)
        elif entry == "native":
            from ultrakb.runtime.records import UnitResult

            process = multiprocessing.get_context("spawn").Process(
                target=_execute_native, args=(root, scenario)
            )
            process.start()
            try:
                process.join(timeout=30)
                assert process.exitcode == 0, f"Native worker exit: {process.exitcode}"
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            result = UnitResult.from_summary(
                json.loads((root.parent / "native-generation-result.json").read_text())
            )
            assert result.status == ("completed" if scenario == "pending" else "failed"), (
                result.generation
            )
            receipt = result.generation
            assert result.model_usage == receipt["model_usage"]
            assert result.revision == (
                storage_scope(root, identity).publication_id if scenario == "pending" else None
            )
            retained = UnitResult.from_summary(json.loads(json.dumps(result.summary())))
            assert retained.generation == receipt
        else:
            with TestClient(api.create_app()) as client:
                response = client.post(
                    "/api/v1/deck",
                    json={
                        "kb": str(root),
                        "name": "value",
                        "intent": "Present value.",
                        "stream": entry == "sse",
                        "view_id": "legacy",
                    },
                )
            assert response.status_code == (
                500 if scenario == "publication" and entry == "http" else 200
            ), response.text
            if entry == "sse":
                final = next(
                    chunk
                    for chunk in response.text.split("\n\n")
                    if chunk.startswith(
                        "event: final\n" if scenario == "pending" else "event: error\n"
                    )
                )
                receipt = json.loads(final.split("data: ", 1)[1])["generation"]
            else:
                receipt = response.json().get("generation")
        assert receipt is not None
        if scenario == "pending":
            assert receipt["status"] == "completed" and receipt["projection_pending"]
            assert receipt["publication_id"] == storage_scope(root, identity).publication_id
            assert set(receipt["resources"]) == {str(root / target), str(root / note)}
            assert not (root / target).exists()
        else:
            assert receipt["status"] == "failed" and receipt["candidate_revision_id"], receipt
            assert receipt["publication_id"] is None
            assert receipt["resources"] == [str(root / target)]
            assert receipt["unfinished"]
            assert storage_scope(root, identity).publication_id == scope.publication_id
        assert receipt["generation_id"] and receipt["execution_id"]
        assert receipt["model_usage"]["current"]["requests"] == len(service.requests) == 3
        assert ArtifactStore(root, identity).read(target) == GOOD_DECK.encode()
        assert not (root / note).exists()


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_cli_keeps_deck_validation_exit_status_with_json_and_pending_projection(
    tmp_path, monkeypatch, json_output, pending
):
    from click.testing import CliRunner

    import ultrakb.cli as cli

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
    monkeypatch.setattr(cli, "_selected_scope", lambda *args: scope)

    def unavailable(*args, **kwargs):
        raise OSError("Projection unavailable")

    if pending:
        monkeypatch.setattr(ArtifactStore, "sync", unavailable)
    responses = [
        tool_response(
            "write_file",
            {
                "path": "output/decks/value/index.html",
                "content": "<html><p>Invalid deck</p></html>",
            },
            "invalid-deck",
        ),
        {"content": "Delivered."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = CliRunner().invoke(
            cli.cli,
            ["deck", "new", "value", "Present value.", *(["--json"] if json_output else [])],
        )
    assert result.exit_code == 1, result.output
    if json_output:
        receipt = json.loads(result.output)
        assert receipt["status"] == "completed" and receipt["validation"]["errors"]
        assert receipt["projection_pending"] is pending
    else:
        assert "[ERROR]" in result.output


def test_unavailable_main_scope_is_a_streamed_error_before_any_model_work(tmp_path, monkeypatch):
    from dataclasses import replace

    import ultrakb.api as api
    import ultrakb.api_views as views

    root, identity = deck_knowledge(tmp_path)
    stale = replace(identity, generation="0" * 32)
    monkeypatch.setattr(api, "_resolve_kb", lambda *args: root)
    monkeypatch.setattr(
        views, "view_scope", lambda root, view_id: storage_scope(root, stale, view_id=view_id)
    )
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    with TestClient(api.create_app()) as client:
        response = client.post(
            "/api/v1/deck",
            json={"kb": str(root), "name": "value", "intent": "Present.", "view_id": "legacy"},
        )
    assert response.status_code == 200
    errors = [
        json.loads(chunk.split("data: ", 1)[1])
        for chunk in response.text.split("\n\n")
        if chunk.startswith("event: error\n")
    ]
    assert len(errors) == 1 and errors[0]["code"] == 400
    assert response.text.endswith("event: done\ndata: {}\n\n")


def _execute_native(root, scenario):
    from uuid import uuid4

    import ultrakb.application.views as views
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import preview_generation
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import GenerateArtifact
    from ultrakb.runtime.worker import _execute
    from ultrakb.storage.database import open_database

    with open_database(root, read_only=True) as db:
        identity = db.identity
    scope = storage_scope(root, identity)

    def unavailable(*args, **kwargs):
        raise OSError("Projection unavailable")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(views, "view_scope", lambda *args: scope)
        if scenario == "pending":
            patch.setattr(ArtifactStore, "sync", unavailable)
            patch.setattr(ProjectionStore, "sync", unavailable)
        version = preview_generation(root, "deck", "value", scope=scope).version
        request = GenerateArtifact("deck", "value", "Present value.", version, view_id="legacy")
        result = _execute(
            request,
            UnitIdentity.create(uuid4().hex, 0, str(root), request),
            ExecutionContext(),
        )
    (root.parent / "native-generation-result.json").write_text(json.dumps(result.summary()))
