"""Shared maintenance operations preserve a portable store and its recovery state."""

import json

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.pages import read_page
from ultrakb.application.views import storage_scope


def test_backup_and_restore_preserve_the_kb_configuration_pair_and_fixed_knowledge(tmp_path):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge

    root, identity, _ = question_knowledge(tmp_path)
    credentials = b"LLM_API_KEY=backup-fixture-key\nOPENAI_API_BASE=http://127.0.0.1:1\n"
    (root / ".env").write_bytes(credentials)
    configuration = (root / ".ultrakb/config.yaml").read_bytes()
    backup = backup_knowledge(root, tmp_path / "backup", scope=storage_scope(root, identity))
    assert backup.coverage["configuration"] == "complete"
    restored = restore_knowledge(tmp_path / "backup", tmp_path / "restored")
    assert restored.identity.generation != identity.generation
    assert (tmp_path / "restored/.env").read_bytes() == credentials
    assert (tmp_path / "restored/.ultrakb/config.yaml").read_bytes() == configuration
    scope = storage_scope(tmp_path / "restored", restored.identity)
    assert (
        read_page(tmp_path / "restored", "sources/fixture.md", scope=scope).body
        == "The value is 17."
    )


@pytest.mark.parametrize("damage", ["bytes", "manifest", "missing_files"])
def test_restore_rejects_damaged_configuration_before_creating_the_destination(tmp_path, damage):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge

    root, identity, _ = question_knowledge(tmp_path)
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup_knowledge(root, archive, scope=storage_scope(root, identity))
    if damage == "bytes":
        (archive / ".ultrakb/config.yaml").write_text("model: unexpected\n")
    else:
        manifest = json.loads((archive / "backup.json").read_text())
        if damage == "manifest":
            manifest["files"][0]["path"] = "../escape"
        else:
            manifest["files"] = []
        (archive / "backup.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore_knowledge(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / "escape").exists()


def test_backup_restores_paused_changes_deletions_and_unknown_files_without_accepting_them(
    tmp_path,
):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.application.wiki_worktree import accept_external_edit, begin_external_edit
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, initial = question_knowledge(tmp_path)
    assert ProjectionStore(root, identity).sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "index.md").write_text("Manual draft")
    (edit.directory / "sources/fixture.md").unlink()
    (edit.directory / "unknown.md").write_text("Unaccepted new page")
    knowledge = KnowledgeStore(root, identity)
    previous = knowledge.bind()
    knowledge.publish(
        knowledge.prepare_revision(
            {
                **{path: previous.read(path) for path in previous.files()},
                "new.md": b"Published while paused",
            },
            expected=initial.publication_id,
        )
    )
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    result = backup_knowledge(root, archive, scope=storage_scope(root, identity))
    assert result.coverage["worktree"] == "complete"
    restored = restore_knowledge(archive, destination)
    restarted = begin_external_edit(destination, restored.identity)
    assert restarted.edit_id == edit.edit_id
    assert restarted.baseline == edit.baseline
    assert (restarted.directory / "index.md").read_text() == "Manual draft"
    assert not (restarted.directory / "sources/fixture.md").exists()
    assert (restarted.directory / "unknown.md").read_text() == "Unaccepted new page"
    assert ProjectionStore(destination, restored.identity).sync().status == "paused"
    scope = storage_scope(destination, restored.identity)
    assert read_page(destination, "sources/fixture.md", scope=scope).body == "The value is 17."
    accepted = accept_external_edit(
        destination, restored.identity, restarted.edit_id, editor_stopped=True
    )
    assert accepted.status == "accepted"
    current = storage_scope(destination, restored.identity)
    assert read_page(destination, "index.md", scope=current).body == "Manual draft"
    assert read_page(destination, "new.md", scope=current).body == "Published while paused"


def test_restore_checks_the_actual_copied_paused_payload_before_publishing(tmp_path, monkeypatch):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.storage import backup_worktree
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, _ = question_knowledge(tmp_path)
    assert ProjectionStore(root, identity).sync().status == "synced"
    edit = begin_external_edit(root, identity)
    (edit.directory / "index.md").write_text("Original paused bytes")
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup_knowledge(root, archive, scope=storage_scope(root, identity))
    copy = backup_worktree._copy_file_atomic

    def change_after_validation(source, target, **options):
        if "backup-state" in source.parts:
            source.write_bytes(b"Changed after archive validation")
        return copy(source, target, **options)

    monkeypatch.setattr(backup_worktree, "_copy_file_atomic", change_after_validation)
    with pytest.raises(ValueError, match="digest"):
        restore_knowledge(archive, destination)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_backup_restores_pending_provider_response_before_changing_generation(tmp_path):
    from stream_transport import StreamTransport

    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_backup_response BEFORE INSERT ON ukb_response_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected response failure'); END"
        )
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport([{"content": "Received once, then retained for repair."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", context=context, scope=storage_scope(root, identity)
        )
        assert result.status == "blocked"
        assert len(service.requests) == 1
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_backup_response")
    assert list(ReceiptStore(root, identity).pending.glob("*.json"))
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup = backup_knowledge(root, archive, scope=storage_scope(root, identity))
    assert backup.coverage["external_receipts"] == "complete"
    restored = restore_knowledge(archive, destination)
    [request] = (
        UsageStore(destination, restored.identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    response = ReceiptStore(destination, restored.identity).read(request.request_attempt_id)
    assert b"Received once, then retained for repair." in response
    assert request.send_status == "sent"
    assert not list(ReceiptStore(destination, restored.identity).pending.glob("*.json"))
    with pytest.raises(ValueError, match="generation"):
        ReceiptStore(destination, restored.identity).capture(
            request.request_attempt_id, b"late old response", version=999, usage={}
        )


def test_unknown_pending_receipt_is_retained_with_explicit_incomplete_coverage(tmp_path):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.storage.receipt_store import ReceiptStore

    root, identity, _ = question_knowledge(tmp_path)
    pending = ReceiptStore(root, identity).pending
    pending.mkdir(parents=True)
    (pending / "unknown.json").write_bytes(b"Unconfirmed response with unknown ownership")
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup = backup_knowledge(root, archive, scope=storage_scope(root, identity))
    assert backup.coverage["external_receipts"] == "partial"
    restored = restore_knowledge(archive, destination)
    assert restored.coverage["external_receipts"] == "partial"
    assert (ReceiptStore(destination, restored.identity).pending / "unknown.json").read_bytes() == (
        b"Unconfirmed response with unknown ownership"
    )


@pytest.mark.asyncio
async def test_restore_interrupts_running_work_without_resetting_its_paid_budget(tmp_path):
    import asyncio
    from threading import Event

    from stream_transport import StreamTransport

    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.usage_store import UsageStore

    root, identity, _ = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    gate = Event()
    with StreamTransport([{"content": "The value is 17."}], gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        running = asyncio.create_task(
            ask_question(
                root, "Read the value.", context=context, scope=storage_scope(root, identity)
            )
        )
        try:
            for _ in range(250):
                if service.requests:
                    break
                await asyncio.sleep(0.01)
            assert len(service.requests) == 1
            archive, destination = tmp_path / "backup", tmp_path / "restored"
            backup_knowledge(root, archive, scope=storage_scope(root, identity))
            restored = restore_knowledge(archive, destination)
        finally:
            gate.set()
            await running
    previous = context.storage_execution
    store = ExecutionStore(destination, restored.identity)
    assert store.status(previous.execution_id) == "interrupted"
    with storage_stage(destination, restored.identity, read_only=True) as db:
        states = db.connection.execute(
            "SELECT state, ended_at, duration_seconds FROM ukb_stage_attempts "
            "WHERE execution_id = ?",
            (previous.execution_id,),
        ).fetchall()
        assert states
        assert all(state != "started" for state, _, _ in states)
        assert any(row == ("interrupted", None, None) for row in states)
    budget = store.budget(previous.model_budget_id)
    assert budget.used == 1
    [request] = (
        UsageStore(destination, restored.identity)
        .requests(execution_id=previous.execution_id)
        .records
    )
    assert request.state == "interrupted"
    assert request.ended_at is None
    assert (
        UsageStore(destination, restored.identity).summary(execution_id=previous.execution_id)[
            "in_flight_requests"
        ]
        == 0
    )
    continued = store.begin(
        "model_task",
        fingerprint=context.executor.fingerprint,
        max_calls=budget.max_calls,
        deadline_at=budget.deadline_at,
        resume=previous.execution_id,
    )
    assert continued.execution_id != previous.execution_id
    assert continued.model_budget_id == previous.model_budget_id
    assert store.budget(continued.model_budget_id).used == 1


@pytest.mark.asyncio
async def test_restored_compiler_resumes_accepted_results_with_a_new_execution_and_no_resend(
    tmp_path, monkeypatch
):
    import asyncio

    from stream_transport import StreamTransport

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.recompilation import recompile_document
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    publish = KnowledgeStore.publish

    def fail_original_publication(store, *args, **options):
        if store.root == root:
            raise OSError("Publication interrupted after paid computation")
        return publish(store, *args, **options)

    monkeypatch.setattr(KnowledgeStore, "publish", fail_original_publication)
    context = ExecutionContext(storage_identity=identity)
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    with StreamTransport([{"content": summary}, {"content": plan}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        imported = await asyncio.to_thread(
            import_document, root, source, context=context, scope=storage_scope(root, identity)
        )
        assert imported.status == "blocked"
        [unit] = imported.units
        assert len(service.requests) == 2
        archive, destination = tmp_path / "backup", tmp_path / "restored"
        backup_knowledge(root, archive, scope=storage_scope(root, identity))
        restored = restore_knowledge(archive, destination)
        continued = ExecutionContext(
            storage_identity=restored.identity,
            resume_storage_execution_id=context.storage_execution.execution_id,
        )
        result = await recompile_document(
            destination,
            imported.source_id,
            unit_id=unit.unit_id,
            context=continued,
            scope=storage_scope(destination, restored.identity),
        )
        assert result.status == "compiled", result.message
        assert len(service.requests) == 2
    assert continued.storage_execution.execution_id != context.storage_execution.execution_id
    assert continued.storage_execution.model_budget_id == context.storage_execution.model_budget_id
    store = ExecutionStore(destination, restored.identity)
    assert store.budget(continued.storage_execution.model_budget_id).used == 2
    assert (
        "17"
        in read_page(
            destination,
            f"summaries/{unit.doc_name}.md",
            scope=storage_scope(destination, restored.identity),
        ).body
    )
