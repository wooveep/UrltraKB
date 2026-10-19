"""Native source withdrawal checks confirmation and retains immutable evidence."""

import os

from test_storage_source_removal import retained_publication

from ultrakb.application.removal import preview_removal, remove_document
from ultrakb.application.views import published_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_projection import ProjectionStore


def test_changed_plan_requires_another_confirmation(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)
    store = KnowledgeStore(root, identity)
    publication = store.publish(
        store.prepare_changes(
            scope.knowledge, {"concepts/manual.md": b"Human knowledge committed after the preview"}
        )
    )
    conflict = remove_document(root, unit.source_id, version=preview.version)
    assert conflict.status == "conflict"
    assert not CatalogStore(root, identity).source(unit.source_id).removed
    current = published_scope(root)
    assert current.publication_id == publication.publication_id
    latest = preview_removal(root, unit.source_id, scope=current)
    assert latest.version != preview.version
    assert remove_document(root, unit.source_id, version=latest.version).status == "removed"
    assert store.bind().read("concepts/manual.md") == b"Human knowledge committed after the preview"
    assert (
        CatalogStore(root, identity).read_original(unit.source_revision_id) == b"The value is 17."
    )


def test_withdrawal_needs_no_model_configuration_and_changes_no_environment(tmp_path):
    root, identity, unit, scope = retained_publication(tmp_path)
    before = {path: scope.knowledge.read(path) for path in scope.knowledge.files()}
    (root / ".ultrakb/config.yaml").unlink()
    environment = dict(os.environ)
    result = remove_document(root, unit.source_id, scope=scope)
    assert result.status == "removed"
    assert CatalogStore(root, identity).source(unit.source_id).removed
    assert {path: published_scope(root).knowledge.read(path) for path in before} == before
    assert (
        CatalogStore(root, identity).read_original(unit.source_revision_id) == b"The value is 17."
    )
    assert dict(os.environ) == environment


def test_projection_failure_keeps_committed_withdrawal_and_replays_only_sync(tmp_path, monkeypatch):
    root, identity, unit, scope = retained_publication(tmp_path)
    preview = preview_removal(root, unit.source_id, scope=scope)

    def failure(*args, **options):
        raise OSError("Injected projection I/O failure")

    with monkeypatch.context() as fault:
        fault.setattr(ProjectionStore, "sync", failure)
        result = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert result.status == "removed" and result.projection_pending
    assert CatalogStore(root, identity).source(unit.source_id).removed
    assert result.publication_id == KnowledgeStore(root, identity).bind().publication_id
    resumed = remove_document(root, unit.source_id, version=preview.version, scope=scope)
    assert resumed.status == "removed" and not resumed.projection_pending
    assert resumed.publication_id == result.publication_id
