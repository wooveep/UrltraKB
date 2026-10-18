"""Real persisted pending work keeps unit success independent of a sibling failure."""


def test_pending_retry_preserves_sibling_publication_and_prior_attempt(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.pending_store import PendingStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog, pending = CatalogStore(root, identity), PendingStore(root, identity)
    source, body = tmp_path / "workbook.xlsx", tmp_path / "sheet.md"
    source.write_bytes(b"Workbook original")
    body.write_text("Worksheet text")
    original = catalog.admit(source, source_key="workbook")

    def unit(key):
        processing = catalog.retain_processing(
            original.revision_id,
            {"body.md": body},
            body_path="body.md",
            fingerprint="worksheet-v1",
            operation_id=key,
            decision=classify_markdown_tokens(2),
        )
        return catalog.plan_unit(processing.processing_id, key=key, doc_name=key)

    sales, costs = unit("sales"), unit("costs")
    sales_job = pending.enqueue(original.revision_id, unit_revision_id=sales.unit_revision_id)
    costs_job = pending.enqueue(
        original.revision_id,
        unit_revision_id=costs.unit_revision_id,
        root_import_id=sales_job.root_import_id,
    )
    running = pending.start(pending.claim())
    assert running.intent_id == sales_job.intent_id
    knowledge = KnowledgeStore(root, identity)
    publication = knowledge.publish(
        knowledge.prepare_revision({"sales.md": b"Sales"}, expected=None)
    )
    assert pending.finish(running, status="completed", publication_id=publication.publication_id)
    failed = pending.start(pending.claim())
    assert failed.intent_id == costs_job.intent_id
    assert pending.finish(failed, status="failed", error_code="index_failed")
    pending.retry(costs_job.intent_id)
    restarted = PendingStore(root, identity)
    retry = restarted.start(restarted.claim())
    assert retry.attempt_id != failed.attempt_id
    assert retry.root_import_id == failed.root_import_id
    assert [attempt.status for attempt in restarted.attempts(costs_job.intent_id)] == [
        "failed",
        "started",
    ]
    assert restarted.job(sales_job.intent_id).status == "completed"
    assert knowledge.bind(publication.publication_id).read("sales.md") == b"Sales"


def test_retry_rejects_old_callbacks_and_keeps_the_group_budget(tmp_path):
    import pytest

    from ultrakb.pending.records import ExecutionBudget
    from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.pending_store import PendingStore

    root = tmp_path / "kb"
    identity = create_database(root)
    original = tmp_path / "original.md"
    original.write_text("managed input")
    revision = CatalogStore(root, identity).admit(original, source_key="original")
    store = PendingStore(root, identity)
    budget = ExecutionBudget(max_sources=2)
    job = store.enqueue(revision.revision_id, budget=budget)
    first = store.start(store.claim())
    assert store.finish(first, status="failed", error_code="temporary")
    store.retry(job.intent_id)
    second = store.start(store.claim())
    assert not store.finish(first, status="failed", error_code="late")
    assert store.job(job.intent_id) == second
    assert store.budget(job.root_import_id) == budget
    with pytest.raises(CatalogConflict, match="budget"):
        store.enqueue(
            revision.revision_id,
            root_import_id=job.root_import_id,
            budget=ExecutionBudget(max_sources=10),
        )
    store.cancel(job.root_import_id)
    assert store.finish(second, status="stopped")
    with pytest.raises(CatalogConflict, match="retried"):
        store.retry(job.intent_id)


def test_a_changed_source_is_rejected_before_starting_work(tmp_path):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.pending_store import PendingStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog, store = CatalogStore(root, identity), PendingStore(root, identity)
    original = tmp_path / "input.md"
    original.write_text("old input")
    first = catalog.admit(original, source_key="input")
    job = store.enqueue(first.revision_id)
    claimed = store.claim()
    original.write_text("changed input")
    catalog.admit(original, source_key="input", expected=first.revision_id)
    assert store.start(claimed) is None
    assert store.job(job.intent_id).status == "stale"
    assert store.attempts(job.intent_id) == ()
