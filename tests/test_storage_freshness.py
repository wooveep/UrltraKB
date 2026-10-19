"""Knowledge validity follows retained evidence, independently of page contents."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input
from test_storage_document_management import replace_target
from test_storage_source_removal import retained_publication

from ultrakb.application.knowledge_bases import get_kb_status
from ultrakb.application.pages import read_page
from ultrakb.application.query_views import read_query_page, resolve_query_views
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.refresh import refresh_status
from ultrakb.application.removal import remove_document
from ultrakb.application.views import storage_scope
from ultrakb.locks import atomic_write_text
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["original", "processing"])
async def test_changed_target_marks_actual_page_basis_without_rewriting_it(tmp_path, change):
    root, identity, unit = compile_input(tmp_path)
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
        assert result.status == "compiled", result.message
        scope = storage_scope(root, identity)
        original = read_page(root, "summaries/fixture.md", scope=scope)
        if change == "original":
            replacement = tmp_path / "updated.txt"
            replacement.write_text("The value is now 29.")
            target = (
                CatalogStore(root, identity)
                .admit(replacement, source_key="fixture", expected=unit.source_revision_id)
                .revision_id
            )
        else:
            replace_target(root, identity, unit, tmp_path)
            target = unit.source_revision_id

        changed = read_page(root, "summaries/fixture.md", scope=scope)
        assert changed.content == original.content and changed.version == original.version
        assert changed.validity == "needs_refresh"
        assert changed.source_revision_ids == (unit.source_revision_id,)
        [reason] = changed.refresh_reasons
        assert reason["kind"] == "updated" and reason["source_id"] == unit.source_id
        assert reason["target_revision_id"] == target
        status = refresh_status(root, scope=scope)
        assert status["publication_id"] == scope.publication_id
        assert status["needs_refresh"]["summaries/fixture.md"] == [reason]
        assert status["effective_inputs"] == ()
        assert get_kb_status(root, scope=scope)["last_compile"] is not None
        selection = resolve_query_views(root, scope=scope)
        assert selection.views[0].validity == "needs_refresh"
        rendered = read_query_page(selection, "summaries/fixture.md", view_id=scope.view_id)
        assert "status=needs_refresh" in rendered and unit.source_revision_id in rendered
        assert "The value is 17." in rendered
        assert len(service.requests) == 2


def test_withdrawal_keeps_fixed_bytes_and_reports_live_pending_reasons(tmp_path):
    root, identity, unit, old_scope = retained_publication(tmp_path)
    removed = remove_document(root, unit.source_id, scope=old_scope)
    assert removed.status == "removed"
    current = storage_scope(root, identity)
    page = read_page(root, "summaries/fixture.md", scope=current)
    assert page.body == "The value is 17." and page.validity == "needs_refresh"
    assert page.source_revision_ids == (unit.source_revision_id,)
    assert [reason["kind"] for reason in page.refresh_reasons] == ["withdrawn"]
    historical = read_page(root, "summaries/fixture.md", scope=old_scope)
    assert historical.content == page.content and historical.validity == "needs_refresh"
    assert historical.publication_id == old_scope.publication_id
    assert refresh_status(root, scope=current)["effective_inputs"] == ()
    assert not (root / ".ultrakb/catalog").exists()


def test_status_counts_committed_resources_and_managed_originals(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    atomic_write_text(root / "wiki/reports/unaccepted.md", "Unaccepted external draft")
    status = get_kb_status(root, scope=scope)
    assert status["publication_id"] == scope.publication_id
    assert status["directories"]["summaries"] == 1
    assert status["directories"]["reports"] == 0
    assert status["raw_count"] == 1 and status["total_indexed"] == 1
    assert status["published_at"] is not None
    assert status["last_compile"] is None and status["last_lint"] is None
    assert status["needs_refresh"] == {}
    removed = remove_document(root, unit.source_id, scope=scope)
    current = storage_scope(root, identity)
    status = get_kb_status(root, scope=current)
    assert status["publication_id"] == removed.publication_id
    assert set(status["needs_refresh"][scope.view_id]) == {"index.md", "summaries/fixture.md"}


def test_new_active_input_does_not_relabel_an_unchanged_shared_page(tmp_path):
    root, identity, first, _ = retained_publication(tmp_path)
    store = KnowledgeStore(root, identity)
    base = store.bind()
    store.publish(
        store.prepare_changes(
            base,
            {"concepts/shared.md": b"Old supported fact"},
            page_inputs={"concepts/shared.md": (first.unit_revision_id,)},
        )
    )
    newer = replace_target(root, identity, first, tmp_path)
    base = store.bind()
    files = {path: base.read(path) for path in base.files()}
    files["summaries/fixture.md"] = b"Updated summary"
    store.publish(
        store.prepare_revision(
            files,
            expected=base.publication_id,
            unit_revisions=(newer.unit_revision_id,),
            page_inputs={
                "index.md": (newer.unit_revision_id,),
                "summaries/fixture.md": (newer.unit_revision_id,),
                "concepts/shared.md": (first.unit_revision_id,),
            },
        )
    )
    current = storage_scope(root, identity)
    summary = read_page(root, "summaries/fixture.md", scope=current)
    shared = read_page(root, "concepts/shared.md", scope=current)
    assert summary.validity == "current" and not summary.refresh_reasons
    assert shared.validity == "needs_refresh" and shared.body == "Old supported fact"
    assert set(refresh_status(root, scope=current)["needs_refresh"]) == {"concepts/shared.md"}
    assert refresh_status(root, scope=current)["effective_inputs"] == (newer.unit_revision_id,)


def test_current_question_keeps_its_selection_validity_when_reading_a_fixed_page(tmp_path):
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.views import view_scope

    root, identity, unit = compile_input(tmp_path, body_text="TLS is required.")
    store = KnowledgeStore(root, identity)
    published = store.publish(
        store.prepare_revision(
            {"index.md": b"[[summaries/manual]]", "summaries/manual.md": b"TLS is required."},
            expected=store.bind().publication_id,
            unit_revisions=(unit.unit_revision_id,),
        )
    )
    selection = resolve_query_views(root, scope=view_scope(root, "legacy"))
    [view] = selection.views
    assert view.validity == "current"
    body = read_query_page(selection, "summaries/manual.md", view_id=view.view_id)
    assert "status=current" in body and "TLS is required." in body
    historical = resolve_query_views(
        root,
        scope=view_scope(root, "legacy", historical_revision=published.revision_id),
    )
    body = read_query_page(historical, "summaries/manual.md", view_id=view.view_id)
    assert "status=historical" in body and "TLS is required." in body
