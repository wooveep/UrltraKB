"""Stopping one dispatch is durable without cancelling its retained import group."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from stream_transport import StreamTransport
from test_storage_discovery import embedded_book, responses
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.pending import (
    claim_pending_job,
    pending_status,
    process_pending,
    retry_pending_job,
    run_pending_job,
)
from ultrakb.application.views import storage_scope
from ultrakb.lifecycle import current_generation
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore
from ultrakb.storage.pending_store import PendingStore

pytestmark = pytest.mark.integration


def test_stopping_a_dispatch_does_not_stop_its_explicit_retry(tmp_path):
    from ultrakb.application.pending import stop_pending_job

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    revision = CatalogStore(root, identity).admit(source, source_key=str(source))
    scope = storage_scope(root, identity)
    DiscoveryStore(root, identity).schedule(
        revision, root_import_id=uuid4().hex, view_id=scope.view_id
    )
    claimed = claim_pending_job(root, scope=scope)
    generation = current_generation(root)
    assert stop_pending_job(root, claimed["id"], claimed["dispatch_id"], generation, scope=scope)
    assert (
        run_pending_job(root, claimed["id"], claimed["dispatch_id"], scope=scope)["status"]
        == "stopped"
    )
    retry_pending_job(root, claimed["id"], scope=scope)
    following = claim_pending_job(root, scope=scope)
    assert following["dispatch_id"] != claimed["dispatch_id"]
    assert not stop_pending_job(
        root, claimed["id"], claimed["dispatch_id"], generation, scope=scope
    )
    result = run_pending_job(root, following["id"], following["dispatch_id"], scope=scope)
    assert result["status"] == "completed"
    assert pending_status(root, scope=scope)["groups"][0]["cancelled"] is False
    assert not (root / ".ultrakb/catalog").exists()


def test_stop_during_a_real_send_keeps_its_usage_and_blocks_the_next_send(tmp_path):
    from ultrakb.application.pending import stop_pending_job

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    gate = Event()
    gate.set()
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY"), gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        import_document(root, source, scope=storage_scope(root, identity))
        process_pending(root, max_jobs=1, scope=storage_scope(root, identity))
        before = storage_scope(root, identity)
        claimed = claim_pending_job(root, scope=before)
        gate.clear()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(
                    run_pending_job, root, claimed["id"], claimed["dispatch_id"], scope=before
                )
                assert service.wait_for_requests(3, timeout=5)
                assert stop_pending_job(
                    root,
                    claimed["id"],
                    claimed["dispatch_id"],
                    current_generation(root),
                    scope=before,
                )
                assert PendingStore(root, identity).job(claimed["id"]).status == "started"
                gate.set()
                outcome = running.result(timeout=20)
        finally:
            gate.set()
        assert outcome["status"] == "stopped", outcome
        assert outcome["result"]["status"] == "stopped", outcome
        assert outcome["result"]["model_usage"]["current"]["actual_sends"] == 1
        assert len(service.requests) == 3
        assert storage_scope(root, identity).publication_id == before.publication_id
        retry_pending_job(root, claimed["id"], scope=storage_scope(root, identity))
        resumed = process_pending(root, scope=storage_scope(root, identity))
        assert resumed["outcomes"][0]["result"]["status"] == "added", resumed
        assert len(service.requests) == 4


def test_unconfirmed_native_stop_survives_restart_until_explicit_confirmation(
    tmp_path, monkeypatch
):
    import sqlite3

    import ultrakb.application.views as views
    from ultrakb.runtime.requests import RunPendingJob
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    revision = CatalogStore(root, identity).admit(source, source_key=str(source))
    scope = storage_scope(root, identity)
    work = DiscoveryStore(root, identity).schedule(
        revision, root_import_id=uuid4().hex, view_id=scope.view_id
    )
    claimed = claim_pending_job(root, scope=scope)
    monkeypatch.setattr(views, "view_scope", lambda *args: scope)
    history = tmp_path / "history"
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_stop BEFORE UPDATE OF status ON ukb_import_intents "
            "WHEN NEW.status = 'stopped' BEGIN SELECT RAISE(ABORT, 'stop unavailable'); END"
        )
    manager = TaskManager(history_dir=history)
    try:
        with manager._condition:
            task = manager.submit(
                root, [RunPendingJob(work.intent_id, claimed["dispatch_id"], view_id=scope.view_id)]
            )
            controls = []
            manager.stop(task, schedule_control=controls.append)
            manager.stop(task, schedule_control=controls.append)
            assert len(controls) == 1
            with pytest.raises(sqlite3.IntegrityError, match="stop unavailable"):
                controls[0]()
        assert manager.has_work(root)
        assert manager.get(task).pending_stops
        with pytest.raises(ValueError):
            manager.clear_history([task])
        assert (
            PendingStore(root, identity).job(work.intent_id).dispatch_id == claimed["dispatch_id"]
        )
    finally:
        manager.shutdown()
        assert manager.join(5)
    recovered = TaskManager(history_dir=history)
    try:
        assert recovered.has_work(root) and recovered.get(task).pending_stops
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_stop")
        recovered.stop(task)
        assert not recovered.has_work(root) and not recovered.get(task).pending_stops
        assert recovered.get(task).stage == "stopped"
        assert PendingStore(root, identity).job(work.intent_id).status == "stopped"
        assert claim_pending_job(root, scope=scope) is None
        recovered.clear_history([task])
    finally:
        recovered.shutdown(stop=True)
        assert recovered.join(5)
