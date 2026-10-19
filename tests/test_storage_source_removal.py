"""Source withdrawal publishes an exclusion while retaining paid work and evidence."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.removal import preview_removal, remove_document
from ultrakb.application.sources import read_admitted_source, source_inventory
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.database import open_database
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_withdrawal_retains_public_evidence_and_rejects_new_compilation(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        compiled = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
        assert compiled.status == "compiled", compiled.message
        scope = storage_scope(root, identity)
        before = {path: scope.knowledge.read(path) for path in scope.knowledge.files()}
        preview = preview_removal(root, unit.source_id, scope=scope)
        assert preview.status == "ready"
        context = ExecutionContext()
        removed = remove_document(
            root, unit.source_id, version=preview.version, context=context, scope=scope
        )
        assert removed.status == "removed", removed
        assert removed.publication_id != scope.publication_id
        current = storage_scope(root, identity)
        assert current.publication_id == removed.publication_id
        assert {path: current.knowledge.read(path) for path in current.knowledge.files()} == before
        assert scope.knowledge.read("summaries/fixture.md") == before["summaries/fixture.md"]
        source = read_admitted_source(root, unit.source_id, unit_id=unit.unit_id, scope=scope)
        assert source["content"] == "The value is 17."
        assert (
            CatalogStore(root, identity).read_original(unit.source_revision_id)
            == b"The value is 17."
        )
        documents = source_inventory(root, scope=current)
        document = next(item for item in documents if item["source_id"] == unit.source_id)
        assert document["removed"] and document["validity"] == "withdrawn"
        from ultrakb.application.document_actions import document_inventory

        row = next(
            row
            for row in document_inventory(root, scope=current)
            if row.identity.value == unit.source_id
        )
        assert row.actions["read"].allowed and row.actions["ask"].allowed
        assert not any(
            row.actions[action].allowed
            for action in ("retry", "recompile", "reprocess", "remove", "empty")
        )
        stopped = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=current
        )
        assert stopped.status in {"not_found", "blocked"}
        assert len(service.requests) == 2
    from ultrakb.application.knowledge_tasks import read_knowledge_execution

    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert detail.execution.state == "completed"
    assert [row.publication_id for row in detail.publications] == [removed.publication_id]


def retained_publication(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    store.publish(
        store.prepare_revision(
            {"index.md": b"Fixture", "summaries/fixture.md": b"The value is 17."},
            expected=store.bind().publication_id,
            unit_revisions=(unit.unit_revision_id,),
            generated_paths=("summaries/fixture.md",),
        )
    )
    return root, identity, unit, storage_scope(root, identity)


@pytest.mark.asyncio
async def test_withdrawal_during_model_wait_retains_response_and_stops_following_sends(tmp_path):
    from ultrakb.storage.pending_store import PendingStore

    root, identity, unit, scope = retained_publication(tmp_path)
    pending = PendingStore(root, identity)
    queued = pending.enqueue(unit.source_revision_id, unit_revision_id=unit.unit_revision_id)
    claimed = pending.claim(intent_id=queued.intent_id)
    removed = []

    def respond(request, index):
        removed.append(remove_document(root, unit.source_id, scope=scope))
        return {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})}

    context = ExecutionContext()
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
    assert removed[0].status == "removed"
    assert result.status != "compiled"
    assert len(service.requests) == 1
    assert pending.start(claimed) is None
    assert pending.job(queued.intent_id).status == "stale"
    from ultrakb.application.knowledge_tasks import read_knowledge_execution

    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert len(detail.requests) == 1
    assert detail.requests[0].state == "completed"
    assert result.model_usage["current"]["actual_sends"] == 1


@pytest.mark.asyncio
async def test_withdrawal_during_index_wait_prevents_following_index_sends(tmp_path):
    from test_storage_indexing import indexing_response

    from ultrakb.config import load_config, save_config

    root, identity, unit = compile_input(
        tmp_path, segmented=True, body_text="# Fixture\nThe value is 17.\n"
    )
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    scope, removed = storage_scope(root, identity), []

    def respond(request, index):
        if index == 0:
            removed.append(remove_document(root, unit.source_id, scope=scope))
        return indexing_response(request, index)

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(root, unit.source_id, unit_id=unit.unit_id, scope=scope)
    assert removed[0].status == "removed"
    assert result.status != "compiled"
    assert len(service.requests) == 1


def test_exact_withdrawal_replay_recovers_success_without_model_configuration(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)
    (root / ".ultrakb/config.yaml").unlink()
    first = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert first.status == "removed", first
    recovered = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert recovered.status == "removed" and recovered.publication_id == first.publication_id
    assert not recovered.projection_pending
    denied = remove_document(root, unit.source_id, version="changed", scope=scope)
    assert denied.status == "conflict" and denied.publication_id is None
    from ultrakb.storage.database import DatabaseIdentity

    with pytest.raises(ValueError, match="another database generation"):
        remove_document(
            root,
            unit.source_id,
            version=preview.version,
            scope=scope,
            context=ExecutionContext(storage_identity=DatabaseIdentity(identity.kb_id, "0" * 32)),
        )


def test_withdrawal_replay_after_a_synced_successor_has_no_pending_projection(tmp_path):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)
    first = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert first.status == "removed" and not first.projection_pending
    store = KnowledgeStore(root, identity)
    successor = store.prepare_changes(store.bind(), {"reports/later.md": b"Later report"})
    store.publish(successor)
    projection = ProjectionStore(root, identity).sync(plan_id=successor.sync_plan_id)
    assert projection.status == "synced"

    recovered = remove_document(
        root, unit.source_id, version=preview.version, scope=storage_scope(root, identity)
    )
    assert recovered.status == "removed"
    assert recovered.publication_id == first.publication_id
    assert not recovered.projection_pending and not recovered.unfinished
    assert store.bind().publication_id == successor.publication_id


@pytest.mark.parametrize("changed", ["source", "unit", "publication"])
def test_withdrawal_confirmation_rejects_changed_reviewed_targets(tmp_path, changed):
    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)
    catalog, store = CatalogStore(root, identity), KnowledgeStore(root, identity)
    body = tmp_path / "changed.txt"
    body.write_text("The value is 23.")
    if changed == "source":
        catalog.admit(body, source_key="fixture", expected=unit.source_revision_id)
    elif changed == "unit":
        from ultrakb.processing_policy import classify_markdown_tokens

        processing = catalog.retain_processing(
            unit.source_revision_id,
            {"fixture.md": body},
            body_path="fixture.md",
            fingerprint="changed",
            operation_id="changed",
            decision=classify_markdown_tokens(5),
        )
        catalog.plan_unit(processing.processing_id, key="body", doc_name=unit.doc_name)
    else:
        store.publish(store.prepare_changes(scope.knowledge, {"reports/other.md": b"Other"}))
    expected = store.bind().publication_id
    result = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert result.status == "conflict"
    assert not catalog.source(unit.source_id).removed
    assert store.bind().publication_id == expected


@pytest.mark.parametrize(
    "interrupt", ["transaction", "acknowledgement", "projection", "coordination"]
)
def test_withdrawal_recovers_atomic_publication_and_keeps_committed_success(
    tmp_path, monkeypatch, interrupt
):
    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)
    if interrupt == "transaction":
        with open_database(root, expected=identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_withdraw AFTER UPDATE OF state ON ukb_sources "
                "WHEN NEW.state = 'withdrawn' BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
            )
    publish = KnowledgeStore.publish
    companion, retained_companion = root / ".ultrakb/kb.sqlite-shm", tmp_path / "retained-shm"

    def lost_ack(self, prepared, **options):
        publish(self, prepared, **options)
        raise OSError("Publication acknowledgement lost")

    def lost_coordination(self, prepared, **options):
        publication = publish(self, prepared, **options)
        companion.rename(retained_companion)
        return publication

    with monkeypatch.context() as failure:
        if interrupt == "acknowledgement":
            failure.setattr(KnowledgeStore, "publish", lost_ack)
        elif interrupt == "coordination":
            failure.setattr(KnowledgeStore, "publish", lost_coordination)
        elif interrupt == "projection":
            from ultrakb.storage.wiki_projection import ProjectionStore

            def unavailable(*args, **options):
                raise OSError("Projection unavailable")

            failure.setattr(ProjectionStore, "sync", unavailable)
        try:
            result = remove_document(root, unit.source_id, version=preview.version, scope=scope)
        finally:
            if retained_companion.exists():
                retained_companion.rename(companion)
    store, catalog = KnowledgeStore(root, identity), CatalogStore(root, identity)
    if interrupt == "transaction":
        assert result.status == "blocked"
        assert not catalog.source(unit.source_id).removed
        assert store.bind().publication_id == scope.publication_id
        with open_database(root, expected=identity) as db:
            db.connection.execute("DROP TRIGGER fail_withdraw")
        result = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert result.status == "removed", result
    assert catalog.source(unit.source_id).removed
    assert result.publication_id == store.bind().publication_id != scope.publication_id
    assert store.bind().read("summaries/fixture.md") == b"The value is 17."
    if interrupt in {"projection", "coordination"}:
        assert result.projection_pending
