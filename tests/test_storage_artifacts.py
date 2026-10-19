"""Main-store artifacts remain readable independently of their editable projection."""

import pytest
from stream_transport import StreamTransport
from test_storage_conversation_writes import write_call
from test_storage_questions import question_knowledge

from ultrakb.application.artifacts import read_artifact
from ultrakb.application.conversations import continue_conversation
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_actual_model_artifact_is_accepted_then_read_from_its_immutable_original(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    path, content = "output/demo/index.html", "<!doctype html><h1>Accepted artifact</h1>"
    with StreamTransport(
        [write_call("write-html", path, content), {"content": "Created the artifact."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Create an HTML artifact.",
            new_session_id="stored-artifact",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    assert (root / path).read_text() == content
    assert result.resources == (str(root / path),)
    assert result.artifact_outputs[0]["status"] == "saved"
    assert result.artifact_outputs[0]["projection"] == "applied"
    (root / path).unlink()
    assert read_artifact(root, path, scope=storage_scope(root, identity)) == content
    assert not (root / ".ultrakb/published").exists()


@pytest.mark.asyncio
async def test_artifact_projection_failure_retains_master_bytes_and_repairs_without_a_model(
    tmp_path, monkeypatch
):
    import ultrakb.storage.artifact_store as artifact_io

    root, identity, _ = question_knowledge(tmp_path)
    path, content = "output/demo/index.html", "<!doctype html><h1>Durable before sync</h1>"
    replace = artifact_io.atomic_write_bytes

    def unavailable(*args, **kwargs):
        raise OSError("The artifact copy cannot be synchronized")

    monkeypatch.setattr(artifact_io, "atomic_write_bytes", unavailable)
    with StreamTransport(
        [write_call("write-html", path, content), {"content": "The artifact is saved."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Create an HTML artifact.",
            new_session_id="pending-artifact",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    assert result.artifact_outputs[0]["status"] == "saved"
    assert result.artifact_outputs[0]["projection"] == "pending"
    assert not (root / path).exists()
    assert read_artifact(root, path, scope=storage_scope(root, identity)) == content
    monkeypatch.setattr(artifact_io, "atomic_write_bytes", replace)
    repaired = artifact_io.ArtifactStore(root, identity).sync(
        path, revision_id=result.artifact_outputs[0]["revision_id"]
    )
    assert repaired.status == "applied" and (root / path).read_text() == content
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_external_artifact_changes_are_preserved_while_new_master_content_is_saved(tmp_path):
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, _ = question_knowledge(tmp_path)
    path = "output/demo/index.html"
    with StreamTransport(
        [
            write_call("first", path, "First accepted."),
            {"content": "First saved."},
            write_call("second", path, "Second accepted."),
            {"content": "Second saved."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await continue_conversation(
            root,
            "Write the first file.",
            new_session_id="external-artifact",
            scope=storage_scope(root, identity),
        )
        assert first.status == "completed", first.error
        (root / path).write_text("Keep the external edit.")
        second = await continue_conversation(
            root,
            "Update the file.",
            session_id=first.session_id,
            scope=storage_scope(root, identity),
        )
    assert second.status == "completed", second.error
    assert second.artifact_outputs[0]["projection"] == "changed"
    assert (root / path).read_text() == "Keep the external edit."
    assert read_artifact(root, path, scope=storage_scope(root, identity)) == "Second accepted."
    store = ArtifactStore(root, identity)
    assert store.read(path, epoch=first.artifact_outputs[0]["epoch"]) == b"First accepted."
    assert (
        store.sync(path, revision_id=first.artifact_outputs[0]["revision_id"]).status
        == "superseded"
    )
    assert (root / path).read_text() == "Keep the external edit."


@pytest.mark.asyncio
async def test_lost_artifact_acknowledgement_reuses_its_exact_independent_receipt(
    tmp_path, monkeypatch
):
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, _ = question_knowledge(tmp_path)
    save = ArtifactStore.save

    def lost_reply(store, *args, **kwargs):
        save(store, *args, **kwargs)
        raise OSError("Artifact acknowledgement was lost")

    monkeypatch.setattr(ArtifactStore, "save", lost_reply)
    path = "output/demo/recovered.html"
    with StreamTransport(
        [
            write_call("lost-artifact-reply", path, "Accepted before reply loss."),
            {"content": "Saved."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write an artifact.",
            new_session_id="artifact-receipt",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    assert result.artifact_outputs[0]["status"] == "saved"
    assert result.artifact_outputs[0]["projection"] == "applied"
    assert ArtifactStore(root, identity).epoch() == 1
    assert (root / path).read_text() == "Accepted before reply loss."
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_concurrent_artifact_acceptance_retains_the_conflicting_original_for_review(tmp_path):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    store = ArtifactStore(root, identity)
    other = ExecutionStore(root, identity).begin(
        "artifact", fingerprint="other-writer", max_calls=0
    )
    path = "output/demo/race.html"

    def response(request, index):
        if index == 0:
            accepted = store.save(
                path,
                b"Other accepted artifact.",
                expected_epoch=store.epoch(),
                execution_id=other.execution_id,
                evidence_publication_id=publication.publication_id,
            )
            assert accepted.status == "saved"
            assert store.sync(path, revision_id=accepted.revision_id).status == "applied"
            return write_call("racing-artifact", path, "Retain this conflicting artifact.")
        return {"content": "The write conflict cannot be hidden."}

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write an artifact.",
            new_session_id="artifact-conflict",
            scope=storage_scope(root, identity),
        )
    assert result.status == "blocked", result.error
    assert result.resources == ()
    candidate = result.artifact_outputs[0]
    assert candidate["status"] == "conflict" and candidate["epoch"] is None
    assert store.read(path) == b"Other accepted artifact."
    assert (
        store.read(path, revision_id=candidate["revision_id"])
        == b"Retain this conflicting artifact."
    )
    assert (root / path).read_text() == "Other accepted artifact."


def test_artifact_receipt_is_exact_and_restored_callbacks_cannot_accept_new_files(tmp_path):
    from uuid import uuid4

    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.maintenance import Maintenance

    root, identity, publication = question_knowledge(tmp_path)
    executions = ExecutionStore(root, identity)
    execution = executions.begin("artifact", fingerprint="artifact-request", max_calls=0)
    store = ArtifactStore(root, identity)
    path, operation = "output/exact.md", uuid4().hex
    request = {
        "expected_epoch": 0,
        "execution_id": execution.execution_id,
        "evidence_publication_id": publication.publication_id,
        "revision_id": operation,
    }
    accepted = store.save(path, b"Exact original.", **request)
    assert executions.finish(execution.execution_id, state="completed")
    assert store.save(path, b"Exact original.", **request) == accepted
    assert store.epoch() == 1
    with pytest.raises(ValueError, match="identity"):
        store.save(path, b"Different content.", **request)
    backup, restored = tmp_path / "artifact-backup", tmp_path / "artifact-restored"
    Maintenance(root, identity).backup(backup)
    recovered = Maintenance.restore(backup, restored)
    restored_store = ArtifactStore(restored, recovered)
    assert restored_store.read(path, revision_id=accepted.revision_id) == b"Exact original."
    with pytest.raises(ValueError, match="generation"):
        restored_store.save(
            "output/late.md",
            b"Reject the old callback.",
            expected_epoch=1,
            execution_id=execution.execution_id,
            evidence_publication_id=publication.publication_id,
        )
    assert restored_store.epoch() == 1
    with pytest.raises(FileNotFoundError):
        restored_store.read("output/late.md")


def test_persisted_stop_between_artifact_bytes_and_acceptance_rejects_the_late_result(
    tmp_path, monkeypatch
):
    import ultrakb.storage.artifact_store as artifact_io
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    executions = ExecutionStore(root, identity)
    execution = executions.begin("artifact", fingerprint="stop-artifact", max_calls=0)
    accept_bytes = artifact_io.put_blob

    def stop_after_bytes(db, payload):
        blob = accept_bytes(db, payload)
        assert executions.request_stop(execution.execution_id)
        return blob

    monkeypatch.setattr(artifact_io, "put_blob", stop_after_bytes)
    store = artifact_io.ArtifactStore(root, identity)
    with pytest.raises(LockCancelled):
        store.save(
            "output/stopped.md",
            b"Late computation.",
            expected_epoch=0,
            execution_id=execution.execution_id,
            evidence_publication_id=publication.publication_id,
        )
    assert store.epoch() == 0
    with pytest.raises(FileNotFoundError):
        store.read("output/stopped.md")


@pytest.mark.asyncio
async def test_artifact_receipts_survive_failed_turn_acceptance_and_zero_send_recovery(tmp_path):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_artifact_turn BEFORE INSERT ON ukb_turns "
            "BEGIN SELECT RAISE(ABORT, 'Turn not accepted'); END"
        )
    arguments = {"new_session_id": "artifact-turn", "attempt_id": "artifact-submission"}
    path = "output/independent.html"
    with StreamTransport(
        [write_call("independent", path, "Independent content."), {"content": "Full answer."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await continue_conversation(
            root, "Write an artifact.", scope=storage_scope(root, identity), **arguments
        )
        assert failed.status == "failed", failed.error
        assert failed.artifact_outputs[0]["status"] == "saved"
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_artifact_turn")
        recovered = await continue_conversation(
            root, "Write an artifact.", scope=storage_scope(root, identity), **arguments
        )
    assert recovered.status == "completed", recovered.error
    assert recovered.artifact_outputs == failed.artifact_outputs
    assert ArtifactStore(root, identity).epoch() == 1 and len(service.requests) == 2


def test_artifact_durability_is_retried_until_file_and_parent_chain_are_confirmed(
    tmp_path, monkeypatch
):
    import ultrakb.locks as durability
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    execution = ExecutionStore(root, identity).begin(
        "artifact", fingerprint="durable-copy", max_calls=0
    )
    store = ArtifactStore(root, identity)
    path = "output/new/nested/report.md"
    saved = store.save(
        path,
        b"Durable output.",
        expected_epoch=0,
        execution_id=execution.execution_id,
        evidence_publication_id=publication.publication_id,
    )
    sync_directory, attempts = durability._fsync_directory, []

    def failed_directory(directory):
        attempts.append(directory)
        raise OSError("Directory synchronization is unavailable")

    monkeypatch.setattr(durability, "_fsync_directory", failed_directory)
    assert store.sync(path, revision_id=saved.revision_id).status == "pending"
    assert (root / path).read_bytes() == b"Durable output."
    assert store.sync(path, revision_id=saved.revision_id).status == "pending"
    assert len(attempts) >= 2

    def confirmed_directory(directory):
        attempts.append(directory)
        sync_directory(directory)

    attempts.clear()
    monkeypatch.setattr(durability, "_fsync_directory", confirmed_directory)
    assert store.sync(path, revision_id=saved.revision_id).status == "applied"
    assert {root / "output/new/nested", root / "output/new", root / "output", root} <= set(attempts)


def test_unconfirmed_artifact_replacements_remain_recognizable_across_new_heads(
    tmp_path, monkeypatch
):
    import ultrakb.storage.artifact_store as artifact_io
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    execution = ExecutionStore(root, identity).begin(
        "artifact", fingerprint="copy-recovery", max_calls=0
    )
    store, path = artifact_io.ArtifactStore(root, identity), "output/recovery.md"

    def save(content):
        return store.save(
            path,
            content.encode(),
            expected_epoch=store.epoch(),
            execution_id=execution.execution_id,
            evidence_publication_id=publication.publication_id,
        )

    first = save("First confirmed.")
    assert store.sync(path, revision_id=first.revision_id).status == "applied"
    second = save("Second copied before receipt failure.")
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_copy_receipt BEFORE UPDATE OF applied_digest "
            "ON ukb_artifact_heads "
            "BEGIN SELECT RAISE(ABORT, 'Copy receipt failed'); END"
        )
    assert store.sync(path, revision_id=second.revision_id).status == "pending"
    assert (root / path).read_text() == "Second copied before receipt failure."
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_copy_receipt")
    third = save("Third activated but not replaced.")
    replace = artifact_io.atomic_write_bytes

    def interrupted_before_replace(*args, **kwargs):
        raise OSError("Stopped after activation before replacement")

    monkeypatch.setattr(artifact_io, "atomic_write_bytes", interrupted_before_replace)
    assert store.sync(path, revision_id=third.revision_id).status == "pending"
    assert (root / path).read_text() == "Second copied before receipt failure."
    fourth = save("Fourth current target.")
    monkeypatch.setattr(artifact_io, "atomic_write_bytes", replace)
    assert store.sync(path, revision_id=fourth.revision_id).status == "applied"
    assert (root / path).read_text() == "Fourth current target."


def test_never_activated_artifact_content_cannot_authorize_replacing_external_bytes(tmp_path):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    execution = ExecutionStore(root, identity).begin(
        "artifact", fingerprint="activated-only", max_calls=0
    )
    store, path = ArtifactStore(root, identity), "output/external.md"
    request = {
        "execution_id": execution.execution_id,
        "evidence_publication_id": publication.publication_id,
    }
    first = store.save(path, b"First.", expected_epoch=0, **request)
    assert store.sync(path, revision_id=first.revision_id).status == "applied"
    store.save(path, b"Never activated.", expected_epoch=1, **request)
    (root / path).write_bytes(b"Never activated.")
    third = store.save(path, b"Current target.", expected_epoch=2, **request)
    assert store.sync(path, revision_id=third.revision_id).status == "changed"
    assert (root / path).read_bytes() == b"Never activated."


def test_http_output_reads_retained_bytes_and_never_serves_unaccepted_worktree(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from test_storage_artifact_exports import artifact_writer

    from ultrakb.api import create_app

    root, _, _, save = artifact_writer(tmp_path)
    first = save("output/report.html", b"<h1>First accepted</h1>")
    second = save("output/report.html", b"<h1>Current accepted</h1>")
    assert first.status == second.status == "saved"
    copy = root / "output/report.html"
    copy.parent.mkdir(exist_ok=True)
    copy.write_text("<h1>Unaccepted external draft</h1>")
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "artifact-test-token")
    monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda value: root)
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer artifact-test-token"}
    params = {"kb": "fixture", "path": "output/report.html"}
    current = client.get("/api/v1/output", params=params, headers=headers)
    assert current.status_code == 200
    assert current.text == "<h1>Current accepted</h1>"
    historical = client.get(
        "/api/v1/output", params={**params, "revision_id": first.revision_id}, headers=headers
    )
    assert historical.status_code == 200 and historical.text == "<h1>First accepted</h1>"
    copy.unlink()
    assert client.get("/api/v1/output", params=params, headers=headers).text == current.text
    (root / "output/unaccepted.html").write_text("Do not serve this draft")
    missing = client.get(
        "/api/v1/output", params={**params, "path": "output/unaccepted.html"}, headers=headers
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_backup_restore_rebuilds_an_applied_artifact_copy_without_a_model(tmp_path):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, _ = question_knowledge(tmp_path)
    path, content = "output/demo/index.html", "<!doctype html><h1>Restored artifact</h1>"
    with StreamTransport(
        [write_call("write-restored", path, content), {"content": "Saved the artifact."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Create an HTML artifact.",
            new_session_id="restore-artifact",
            scope=storage_scope(root, identity),
        )
        assert result.status == "completed", result.error
        assert result.artifact_outputs[0]["projection"] == "applied"
        backup = tmp_path / "artifact-backup"
        backup_knowledge(root, backup)
        destination = tmp_path / "artifact-restored"
        restored = restore_knowledge(backup, destination)
        assert restored.identity.generation != identity.generation
        scope = storage_scope(destination, restored.identity)
        assert read_artifact(destination, path, scope=scope) == content
        synced = ArtifactStore(destination, restored.identity).sync(path)
        assert synced.status == "applied", synced
        assert (destination / path).read_text() == content
        assert len(service.requests) == 2
