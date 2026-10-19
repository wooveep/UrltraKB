"""Reviewed source batches advance through their own publications and retain successes."""

import pytest
from test_storage_source_removal import retained_publication

from ultrakb.application.batch_removal import (
    BatchRemovalPreview,
    execute_batch_removal,
    preview_batch_removal,
)
from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.views import storage_scope
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def two_sources(tmp_path):
    root, identity, first, _ = retained_publication(tmp_path)
    catalog, store = CatalogStore(root, identity), KnowledgeStore(root, identity)
    body = tmp_path / "second.txt"
    body.write_text("The second value is 29.")
    revision = catalog.admit(body, source_key="second")
    processing = catalog.retain_processing(
        revision.revision_id,
        {"second.md": body},
        body_path="second.md",
        fingerprint="second",
        operation_id="second",
        decision=classify_markdown_tokens(6),
    )
    second = catalog.plan_unit(processing.processing_id, key="body", doc_name="second")
    store.publish(
        store.prepare_revision(
            {
                "index.md": b"Both fixtures",
                "summaries/fixture.md": b"The value is 17.",
                "summaries/second.md": b"The second value is 29.",
                "concepts/both.md": (
                    b"---\nsources: [summaries/fixture.md, summaries/second.md]\n---\nBoth values"
                ),
            },
            expected=store.bind().publication_id,
            unit_revisions=(first.unit_revision_id, second.unit_revision_id),
        )
    )
    return root, identity, tuple(sorted((first, second), key=lambda unit: unit.source_id))


@pytest.mark.parametrize(
    "foreign", [False, True], ids=["own-publications", "foreign-between-items"]
)
def test_reviewed_source_batch_retains_first_success_and_never_adopts_a_foreign_head(
    tmp_path, foreign
):
    root, identity, units = two_sources(tmp_path)
    scope = storage_scope(root, identity)
    preview = preview_batch_removal(
        root, [DocumentIdentity("source", unit.source_id) for unit in units], scope=scope
    )
    assert "concepts/both.md" in preview.affected_pages
    assert "concepts/both.md" in preview.lose_all_sources
    preview = BatchRemovalPreview.decode(preview.encode())
    store, changed = KnowledgeStore(root, identity), []

    def observe(event):
        if foreign and event.get("stage") == "removing" and not changed:
            changed.append(
                store.publish(store.prepare_changes(store.bind(), {"reports/other.md": b"Other"}))
            )

    result = execute_batch_removal(preview, context=ExecutionContext(on_event=observe))
    assert result.items[0].status == "completed", result
    assert CatalogStore(root, identity).source(units[0].source_id).removed
    if foreign:
        assert result.status == "partial"
        assert not CatalogStore(root, identity).source(units[1].source_id).removed
        assert store.bind().publication_id == changed[0].publication_id
        replayed = execute_batch_removal(preview)
        assert replayed.status == "partial" and replayed.items[0].status == "completed"
        assert store.bind().publication_id == changed[0].publication_id
    else:
        assert result.status == "completed", result
        assert all(CatalogStore(root, identity).source(unit.source_id).removed for unit in units)
    assert scope.knowledge.read("concepts/both.md") == store.bind().read("concepts/both.md")


def test_reviewed_batch_rejects_a_changed_worksheet_set_before_any_source_is_withdrawn(tmp_path):
    root, identity, units = two_sources(tmp_path)
    scope = storage_scope(root, identity)
    preview = preview_batch_removal(
        root, [DocumentIdentity("source", unit.source_id) for unit in units], scope=scope
    )
    CatalogStore(root, identity).plan_unit(
        units[1].processing_id, key="new-sheet", doc_name="new-sheet"
    )
    result = execute_batch_removal(preview)
    assert result.status == "blocked"
    assert all(not CatalogStore(root, identity).source(unit.source_id).removed for unit in units)
    assert KnowledgeStore(root, identity).bind().publication_id == scope.publication_id


@pytest.mark.parametrize("when", ["before-commit", "between-sources"])
def test_source_batch_cancellation_preserves_completed_prefix_and_replays_it(tmp_path, when):
    root, identity, units = two_sources(tmp_path)
    scope = storage_scope(root, identity)
    preview = preview_batch_removal(
        root, [DocumentIdentity("source", unit.source_id) for unit in units], scope=scope
    )
    stopped = [False]

    def stop(event):
        if (
            when == "before-commit" and event.get("data", {}).get("stage") == "source.withdraw.v1"
        ) or (when == "between-sources" and event.get("stage") == "removing"):
            stopped[0] = True

    result = execute_batch_removal(
        preview, context=ExecutionContext(cancelled=lambda: stopped[0], on_event=stop)
    )
    assert result.status == "stopped", result
    assert sum(item.status == "completed" for item in result.items) == int(
        when == "between-sources"
    )
    recovered = execute_batch_removal(preview)
    assert recovered.status == "completed", recovered
    publication_id = KnowledgeStore(root, identity).bind().publication_id
    again = execute_batch_removal(preview)
    assert again.status == "completed" and again.publication_id == publication_id
    assert KnowledgeStore(root, identity).bind().publication_id == publication_id
