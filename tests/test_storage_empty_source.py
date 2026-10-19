"""Empty contribution confirmation is a retained, generation-bound publication."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_document_management import replace_target
from test_storage_questions import question_knowledge
from test_storage_source_removal import retained_publication

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.refresh import (
    confirm_empty_source,
    read_source_contribution_target,
    refresh_status,
)
from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
from ultrakb.application.sources import source_inventory
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def responses():
    return [
        {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]


@pytest.mark.asyncio
async def test_empty_confirmation_retains_evidence_and_recovers_the_exact_publication(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    generation = catalog.source(unit.source_id).generation
    target = read_source_contribution_target(root, unit.source_id, scope=scope)
    assert target.target_generation == generation
    assert target.source_revision_id == unit.source_revision_id
    (root / ".ultrakb/config.yaml").unlink()
    result = confirm_empty_source(root, unit.source_id, generation=generation, scope=scope)
    assert result.status == "completed", result
    current = storage_scope(root, identity)
    assert current.publication_id == result.publication_id
    source = catalog.source(unit.source_id)
    assert source.contribution_empty and not source.removed
    assert source.generation == generation + 1
    page = read_page(root, "summaries/fixture.md", scope=current)
    assert page.body == "The value is 17." and page.validity == "needs_refresh"
    assert [reason["kind"] for reason in page.refresh_reasons] == ["empty"]
    assert refresh_status(root, scope=current)["effective_inputs"] == ()
    [inventory] = source_inventory(root, scope=current)
    assert inventory["contribution_empty"] and inventory["status"] == "empty"
    assert catalog.read_original(unit.source_revision_id) == b"The value is 17."
    replayed = confirm_empty_source(
        root, unit.source_id, generation=generation, scope=current, context=ExecutionContext()
    )
    assert replayed.status == "completed" and replayed.publication_id == result.publication_id
    refused = await recompile_document(root, unit.source_id, unit_id=unit.unit_id, scope=current)
    assert refused.status == "blocked", refused
    assert not (root / ".ultrakb/catalog").exists()


@pytest.mark.parametrize("changed", ["source", "unit", "publication"])
def test_empty_confirmation_rejects_a_changed_reviewed_target(tmp_path, changed):
    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    generation = catalog.source(unit.source_id).generation
    if changed == "source":
        original = tmp_path / "changed.txt"
        original.write_text("A replacement value")
        catalog.admit(original, source_key="fixture", expected=unit.source_revision_id)
    elif changed == "unit":
        replace_target(root, identity, unit, tmp_path)
    else:
        store = KnowledgeStore(root, identity)
        store.publish(store.prepare_changes(scope.knowledge, {"reports/later.md": b"Later"}))
    head = storage_scope(root, identity).publication_id
    result = confirm_empty_source(root, unit.source_id, generation=generation, scope=scope)
    assert result.status == "conflict", result
    assert not catalog.source(unit.source_id).contribution_empty
    assert storage_scope(root, identity).publication_id == head


def test_new_target_clears_only_the_old_empty_confirmation(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    generation = catalog.source(unit.source_id).generation
    result = confirm_empty_source(root, unit.source_id, generation=generation, scope=scope)
    assert result.status == "completed", result
    original = tmp_path / "new-source.txt"
    original.write_text("The value is now 23.")
    newer = catalog.admit(original, source_key="fixture", expected=unit.source_revision_id)
    assert not catalog.source(unit.source_id).contribution_empty
    assert newer.revision_id != unit.source_revision_id
    denied = confirm_empty_source(
        root, unit.source_id, generation=generation, scope=storage_scope(root, identity)
    )
    assert denied.status == "conflict", denied


def test_late_normalization_cannot_erase_an_empty_confirmation(tmp_path, monkeypatch):
    from ultrakb.application import storage_ingestion

    root, identity, _ = question_knowledge(tmp_path)
    original = tmp_path / "later.md"
    original.write_text("The value is 17.")
    catalog = CatalogStore(root, identity)
    normalize, confirmed = storage_ingestion.normalize_text_document, []

    def finish_old_normalization(*args, **kwargs):
        frozen = normalize(*args, **kwargs)
        [source] = catalog.source_page()
        empty = confirm_empty_source(
            root,
            source.source_id,
            generation=source.generation,
            scope=storage_scope(root, identity),
        )
        assert empty.status == "completed", empty
        confirmed.append((source.source_id, empty.publication_id))
        return frozen

    monkeypatch.setattr(storage_ingestion, "normalize_text_document", finish_old_normalization)
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, original, scope=storage_scope(root, identity))
    assert result.status == "blocked", result
    source_id, publication_id = confirmed[0]
    assert catalog.source(source_id).contribution_empty
    assert catalog.source_units(source_id) == ()
    assert storage_scope(root, identity).publication_id == publication_id
    assert not service.requests


def test_only_fresh_reviewed_reprocessing_can_restore_an_empty_contribution(tmp_path, monkeypatch):
    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    plan_unit, confirmed = CatalogStore.plan_unit, []

    def late_target(self, *args, **kwargs):
        source = self.source(unit.source_id)
        result = confirm_empty_source(
            root, source.source_id, generation=source.generation, scope=scope
        )
        assert result.status == "completed", result
        confirmed.append(result.publication_id)
        return plan_unit(self, *args, **kwargs)

    with StreamTransport(responses()) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        preview = preview_reprocessing(root, unit.source_id, scope=scope)
        with monkeypatch.context() as race:
            race.setattr(CatalogStore, "plan_unit", late_target)
            rejected = reprocess_source(
                root, unit.source_id, version=preview["version"], scope=scope
            )
        assert rejected.status == "blocked", rejected
        assert catalog.source(unit.source_id).contribution_empty
        assert catalog.unit(unit.unit_id) == unit
        assert not service.requests
        current = storage_scope(root, identity)
        assert current.publication_id == confirmed[0]
        fresh = preview_reprocessing(root, unit.source_id, scope=current)
        assert not fresh["resume"] and fresh["version"] != preview["version"]
        restored = reprocess_source(root, unit.source_id, version=fresh["version"], scope=current)
        assert restored.status == "added", restored
        assert not catalog.source(unit.source_id).contribution_empty
        assert catalog.unit(unit.unit_id).unit_revision_id != unit.unit_revision_id
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_empty_confirmation_during_model_wait_retains_response_and_stops_next_send(tmp_path):
    from ultrakb.application.knowledge_tasks import read_knowledge_execution
    from ultrakb.storage.pending_store import PendingStore

    root, identity, unit, scope = retained_publication(tmp_path)
    catalog, pending = CatalogStore(root, identity), PendingStore(root, identity)
    queued = pending.enqueue(unit.source_revision_id, unit_revision_id=unit.unit_revision_id)
    claimed = pending.claim(intent_id=queued.intent_id)
    confirmed = []

    def respond(request, index):
        confirmed.append(
            confirm_empty_source(
                root,
                unit.source_id,
                generation=catalog.source(unit.source_id).generation,
                scope=scope,
            )
        )
        return responses()[0]

    context = ExecutionContext()
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=scope, context=context
        )
    assert confirmed[0].status == "completed", confirmed
    assert result.status != "compiled" and len(service.requests) == 1
    assert pending.start(claimed) is None and pending.job(queued.intent_id).status == "stale"
    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert len(detail.requests) == 1 and detail.requests[0].state == "completed"
    assert result.model_usage["current"]["actual_sends"] == 1


@pytest.mark.parametrize("interrupt", ["transaction", "acknowledgement", "projection", "cleanup"])
def test_empty_confirmation_recovers_atomic_publication_and_preserves_success(
    tmp_path, monkeypatch, interrupt
):
    from ultrakb.storage.database import open_database

    root, identity, unit, scope = retained_publication(tmp_path)
    catalog = CatalogStore(root, identity)
    generation = catalog.source(unit.source_id).generation
    if interrupt == "transaction":
        with open_database(root, expected=identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_empty AFTER UPDATE OF contribution_empty ON ukb_sources "
                "WHEN NEW.contribution_empty = 1 BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
            )
    publish = KnowledgeStore.publish
    companion, retained = root / ".ultrakb/kb.sqlite-shm", tmp_path / "retained-shm"

    def lost_ack(self, prepared, **options):
        publish(self, prepared, **options)
        raise OSError("Publication acknowledgement lost")

    def lost_cleanup(self, prepared, **options):
        publication = publish(self, prepared, **options)
        companion.rename(retained)
        return publication

    with monkeypatch.context() as failure:
        if interrupt == "acknowledgement":
            failure.setattr(KnowledgeStore, "publish", lost_ack)
        elif interrupt == "cleanup":
            failure.setattr(KnowledgeStore, "publish", lost_cleanup)
        elif interrupt == "projection":
            from ultrakb.storage.wiki_projection import ProjectionStore

            def unavailable(*args, **options):
                raise OSError("Projection unavailable")

            failure.setattr(ProjectionStore, "sync", unavailable)
        try:
            result = confirm_empty_source(root, unit.source_id, generation=generation, scope=scope)
        finally:
            if retained.exists():
                retained.rename(companion)
    if interrupt == "transaction":
        assert result.status == "failed", result
        assert not catalog.source(unit.source_id).contribution_empty
        assert storage_scope(root, identity).publication_id == scope.publication_id
        with open_database(root, expected=identity) as db:
            db.connection.execute("DROP TRIGGER fail_empty")
        result = confirm_empty_source(root, unit.source_id, generation=generation, scope=scope)
    assert result.status == "completed", result
    assert catalog.source(unit.source_id).contribution_empty
    assert storage_scope(root, identity).publication_id == result.publication_id
    if interrupt in {"projection", "cleanup"}:
        assert result.projection_pending
    replayed = confirm_empty_source(
        root, unit.source_id, generation=generation, scope=storage_scope(root, identity)
    )
    assert replayed.status == "completed" and replayed.publication_id == result.publication_id
    assert not replayed.projection_pending
