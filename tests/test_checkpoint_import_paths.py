"""Actual index acceptance survives later compiler failure in imports and reprocessing."""

import pytest
from index_recovery_fixtures import index_ids

from ultrakb.application.documents import import_document
from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
from ultrakb.application.source_retry import retry_source
from ultrakb.application.views import published_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.index_store import IndexStore

pytest_plugins = ("index_recovery_fixtures",)


@pytest.mark.parametrize("operation", ["import", "reprocess"])
def test_new_index_is_durable_before_compilation_and_reused_on_retry(
    retained_index, tmp_path, operation
):
    data = retained_index
    before = len(data.service.requests)
    if operation == "import":
        original = tmp_path / "fresh.pdf"
        original.write_bytes(data.original)
        result = import_document(data.root, original)
        original.unlink()
    else:
        preview = preview_reprocessing(data.root, data.target.source_id)
        assert preview["status"] == "ready", preview
        result = reprocess_source(data.root, data.target.source_id, version=preview["version"])
    assert result.status in {"failed", "blocked"}, result.message
    [unit] = result.units
    target = CatalogStore(data.root, data.identity).unit(unit.unit_id)
    [index] = index_ids(data.root, data.identity, target.unit_revision_id)
    assert index != data.index
    document = IndexStore(data.root, data.identity).document(index)
    sent = len(data.service.requests)
    assert sent > before
    assert published_scope(data.root).publication_id == data.publication_id
    again = retry_source(data.root, result.source_id)
    assert again.status in {"failed", "blocked"}, again.message
    assert len(data.service.requests) == sent
    assert IndexStore(data.root, data.identity).document(index) == document
    with storage_stage(data.root, data.identity) as db:
        db.connection.execute("DROP TRIGGER interrupt_summary_generation")
    completed = retry_source(data.root, result.source_id)
    assert completed.status == "added", completed.message
    assert len(data.service.requests) == sent + 2
    assert index in published_scope(data.root).knowledge.index_revisions()
    assert (
        published_scope(data.root).knowledge.read("concepts/operator-note.md")
        == b"# Human note\nKeep the reviewed procedure.\n"
    )
