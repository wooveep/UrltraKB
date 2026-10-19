"""One master commit owns import success, publication receipt and projection activation."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def imported(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.pending_store import PendingStore

    root = tmp_path / "kb"
    identity = create_database(root)
    original = tmp_path / "original.md"
    original.write_text("Managed original")
    catalog = CatalogStore(root, identity)
    source = catalog.admit(original, source_key="source")
    processing = catalog.retain_processing(
        source.revision_id,
        {"body.md": original},
        body_path="body.md",
        fingerprint="policy",
        operation_id="processing",
        decision=classify_markdown_tokens(2),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="original")
    pending = PendingStore(root, identity)
    job = pending.enqueue(source.revision_id, unit_revision_id=unit.unit_revision_id)
    running = pending.start(pending.claim())
    return SimpleNamespace(
        root=root,
        identity=identity,
        original=original,
        catalog=catalog,
        source=source,
        processing=processing,
        unit=unit,
        pending=pending,
        job=job,
        running=running,
        knowledge=KnowledgeStore(root, identity),
    )


def candidate(imported, *, expected=None):
    return imported.knowledge.prepare_revision(
        {"index.md": b"compiled"},
        expected=expected,
        import_work=imported.running,
        unit_revisions=(imported.unit.unit_revision_id,),
    )


def test_import_success_stays_committed_when_worktree_synchronization_fails(imported, monkeypatch):
    from ultrakb.application.publications import publish_import_result
    from ultrakb.storage import wiki_projection

    def failed_worktree(*args):
        raise OSError("injected worktree failure")

    monkeypatch.setattr(wiki_projection, "atomic_write_bytes", failed_worktree)
    result = publish_import_result(
        imported.root,
        imported.identity,
        {"index.md": b"compiled"},
        work=imported.running,
        expected=None,
        unit_revisions=(imported.unit.unit_revision_id,),
    )
    assert result.status == "published"
    assert result.projection.status == "pending"
    assert imported.pending.job(imported.job.intent_id).status == "completed"
    assert imported.pending.attempts(imported.job.intent_id)[0].publication_id == (
        result.publication.publication_id
    )
    assert imported.knowledge.bind().read("index.md") == b"compiled"
    receipt = imported.knowledge.receipt(result.publication.publication_id)
    assert receipt.attempt_id == imported.running.attempt_id
    assert receipt.generation == imported.identity.generation


def test_an_unrelated_publication_cannot_mark_an_import_successful(imported):
    unrelated = imported.knowledge.publish(
        imported.knowledge.prepare_revision({"unrelated.md": b"other"}, expected=None)
    )
    with pytest.raises(ValueError, match="publication transaction"):
        imported.pending.finish(
            imported.running, status="completed", publication_id=unrelated.publication_id
        )
    assert imported.pending.job(imported.job.intent_id).status == "started"


@pytest.mark.parametrize("change", ["source", "unit", "cancel", "retry"])
def test_publication_rechecks_the_retained_claim_in_its_final_commit(imported, change):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogConflict

    prepared = candidate(imported)
    if change == "source":
        imported.original.write_text("Changed managed original")
        imported.catalog.admit(imported.original, source_key="source")
    elif change == "unit":
        processing = imported.catalog.retain_processing(
            imported.source.revision_id,
            {"body.md": imported.original},
            body_path="body.md",
            fingerprint="policy-v2",
            operation_id="processing-v2",
            decision=classify_markdown_tokens(2),
        )
        imported.catalog.plan_unit(processing.processing_id, key="body", doc_name="original")
    elif change == "cancel":
        imported.pending.cancel(imported.job.root_import_id)
    else:
        imported.pending.finish(imported.running, status="interrupted")
        imported.pending.retry(imported.job.intent_id)
        new_attempt = imported.pending.start(imported.pending.claim())
        assert new_attempt.attempt_id != imported.running.attempt_id
    from ultrakb.locks import LockCancelled

    with pytest.raises(LockCancelled if change == "cancel" else CatalogConflict):
        imported.knowledge.publish(prepared)
    with pytest.raises(FileNotFoundError):
        imported.knowledge.bind()
    with pytest.raises(FileNotFoundError):
        imported.knowledge.receipt(prepared.publication_id)
    assert all(
        attempt.publication_id is None
        for attempt in imported.pending.attempts(imported.job.intent_id)
    )


def test_failed_receipt_commit_rolls_back_head_attempt_and_projection_then_can_retry(imported):
    import sqlite3

    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.wiki_projection import ProjectionStore

    old = imported.knowledge.publish(
        imported.knowledge.prepare_revision({"index.md": b"previous"}, expected=None)
    )
    projection = ProjectionStore(imported.root, imported.identity)
    projection.sync()
    prepared = candidate(imported, expected=old.publication_id)
    with storage_stage(imported.root, imported.identity) as db:
        with db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER injected_receipt_failure BEFORE INSERT ON ukb_publication_receipts "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
            )
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        imported.knowledge.publish(prepared)
    assert imported.knowledge.bind().publication_id == old.publication_id
    assert imported.pending.job(imported.job.intent_id).status == "started"
    with pytest.raises(FileNotFoundError):
        imported.knowledge.receipt(prepared.publication_id)
    assert projection.sync(plan_id=prepared.sync_plan_id).status == "obsolete"
    assert (imported.root / "wiki/index.md").read_bytes() == b"previous"
    with storage_stage(imported.root, imported.identity) as db:
        with db.transaction() as connection:
            connection.execute("DROP TRIGGER injected_receipt_failure")
    accepted = imported.knowledge.publish(prepared)
    assert imported.pending.job(imported.job.intent_id).status == "completed"
    assert imported.knowledge.receipt(accepted.publication_id).attempt_id == (
        imported.running.attempt_id
    )
    assert projection.sync(plan_id=prepared.sync_plan_id).status == "synced"
    assert (imported.root / "wiki/index.md").read_bytes() == b"compiled"


def test_success_retry_returns_exact_receipt_without_rewinding_a_newer_publication(imported):
    prepared = candidate(imported)
    first = imported.knowledge.publish(prepared)
    receipt = imported.knowledge.receipt(first.publication_id)
    next_publication = imported.knowledge.publish(
        imported.knowledge.prepare_revision({"index.md": b"later"}, expected=first.publication_id)
    )
    assert imported.knowledge.publish(prepared) == first
    assert imported.knowledge.receipt(first.publication_id) == receipt
    assert imported.knowledge.bind().publication_id == next_publication.publication_id
    assert len(imported.pending.attempts(imported.job.intent_id)) == 1


def test_a_claim_cannot_publish_a_manifest_without_its_unit(imported):
    with pytest.raises(ValueError, match="include the claimed processing unit"):
        imported.knowledge.prepare_revision(
            {"index.md": b"compiled"}, expected=None, import_work=imported.running
        )


def test_a_conflicting_application_publication_returns_a_retained_review_candidate(imported):
    from ultrakb.application.publications import publish_import_result

    current = imported.knowledge.publish(
        imported.knowledge.prepare_revision({"index.md": b"other publication"}, expected=None)
    )
    result = publish_import_result(
        imported.root,
        imported.identity,
        {"index.md": b"compiled"},
        work=imported.running,
        expected=None,
        unit_revisions=(imported.unit.unit_revision_id,),
    )
    assert result.status == "needs_review"
    assert result.reason_code == "knowledge_changed"
    assert result.candidate_revision_id
    assert result.publication is None
    assert imported.pending.job(imported.job.intent_id).status == "started"
    assert imported.knowledge.bind().publication_id == current.publication_id


def test_projection_cannot_turn_a_committed_publication_into_application_failure(
    imported, monkeypatch
):
    from ultrakb.application.publications import publish_import_result
    from ultrakb.storage.wiki_projection import ProjectionStore

    def failed_coordination(*args, **kwargs):
        raise OSError("injected failure before per-file synchronization")

    monkeypatch.setattr(ProjectionStore, "sync", failed_coordination)
    result = publish_import_result(
        imported.root,
        imported.identity,
        {"index.md": b"compiled"},
        work=imported.running,
        expected=None,
        unit_revisions=(imported.unit.unit_revision_id,),
    )
    assert result.status == "published"
    assert result.projection.status == "pending"
    assert result.projection.error_code == "OSError"
    assert imported.pending.job(imported.job.intent_id).status == "completed"
    assert imported.knowledge.bind().publication_id == result.publication.publication_id


def test_completed_import_history_cannot_diverge_from_its_receipt(imported):
    import sqlite3

    from ultrakb.storage.coordination import storage_stage

    first = imported.knowledge.publish(candidate(imported))
    other = imported.knowledge.publish(
        imported.knowledge.prepare_revision({"index.md": b"later"}, expected=first.publication_id)
    )
    with storage_stage(imported.root, imported.identity) as db:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_pending_attempts SET publication_id = ? WHERE attempt_id = ?",
                (other.publication_id, imported.running.attempt_id),
            )
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_pending_attempts (attempt_id, intent_id, dispatch_id, generation, "
                "status, publication_id, error_code, started_at, finished_at) "
                "SELECT 'forged-attempt', intent_id, 'forged-dispatch', generation, status, "
                "publication_id, error_code, started_at, finished_at FROM ukb_pending_attempts "
                "WHERE attempt_id = ?",
                (imported.running.attempt_id,),
            )
    assert (
        imported.pending.attempts(imported.job.intent_id)[0].publication_id == first.publication_id
    )


def test_application_reentry_recovers_success_after_the_response_was_lost(imported):
    from ultrakb.application.publications import publish_import_result

    def publish():
        return publish_import_result(
            imported.root,
            imported.identity,
            {"index.md": b"compiled"},
            work=imported.running,
            expected=None,
            unit_revisions=(imported.unit.unit_revision_id,),
        )

    lost_response = publish()
    later = imported.knowledge.publish(
        imported.knowledge.prepare_revision(
            {"index.md": b"newer publication"}, expected=lost_response.publication.publication_id
        )
    )
    recovered = publish()
    assert recovered.status == "published"
    assert recovered.publication == lost_response.publication
    assert recovered.candidate_revision_id == lost_response.candidate_revision_id
    assert recovered.projection.status == "obsolete"
    assert imported.knowledge.bind().publication_id == later.publication_id


def test_concurrent_reentry_returns_one_committed_outcome(imported, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.application.publications import publish_import_result
    from ultrakb.storage.knowledge_store import KnowledgeStore

    prepared_together = threading.Barrier(2)
    prepare = KnowledgeStore.prepare_revision

    def coordinate_candidates(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_together.wait(timeout=5)
        return prepared

    monkeypatch.setattr(KnowledgeStore, "prepare_revision", coordinate_candidates)

    def publish():
        return publish_import_result(
            imported.root,
            imported.identity,
            {"index.md": b"compiled"},
            work=imported.running,
            expected=None,
            unit_revisions=(imported.unit.unit_revision_id,),
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        calls = [workers.submit(publish) for _ in range(2)]
        results = [call.result(timeout=10) for call in calls]
    assert all(result.status == "published" for result in results)
    assert results[0].publication == results[1].publication
    assert len(imported.pending.attempts(imported.job.intent_id)) == 1


def test_reentry_requires_the_complete_original_claim(imported):
    from dataclasses import replace

    from ultrakb.application.publications import publish_import_result

    committed = imported.knowledge.publish(candidate(imported))
    result = publish_import_result(
        imported.root,
        imported.identity,
        {"index.md": b"compiled"},
        work=replace(imported.running, dispatch_id="another-dispatch"),
        expected=None,
        unit_revisions=(imported.unit.unit_revision_id,),
    )
    assert result.status == "needs_review"
    assert result.publication is None
    assert imported.knowledge.bind().publication_id == committed.publication_id
