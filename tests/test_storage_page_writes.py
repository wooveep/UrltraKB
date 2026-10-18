"""Application edits commit immutable knowledge before projecting editable files."""

from pathlib import Path

import pytest

from ultrakb.application.pages import delete_page, read_page, save_page
from ultrakb.application.views import storage_scope
from ultrakb.storage.database import create_database
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.wiki_projection import ProjectionStore


@pytest.fixture
def published(tmp_path):
    root = tmp_path / "kb"
    identity = create_database(root)
    store = KnowledgeStore(root, identity)
    files = {
        "index.md": (
            b"# Index\n## Concepts\n- [[concepts/one]]: One\n"
            b"- [[concepts/two]]: See [[concepts/one]]\n"
        ),
        "concepts/one.md": b"---\ntype: Concept\ndescription: Original\n---\nOne\n",
        "concepts/two.md": b"See [[concepts/one]] and [[concepts/untouched-ghost]].\n",
        "concepts/other.md": b"Unrelated [[concepts/old-ghost]].\n",
        "sources/image.png": b"retained image",
    }
    publication = store.publish(store.prepare_revision(files, expected=None))
    assert ProjectionStore(root, identity).sync().status == "synced"
    return root, identity, store, publication, files


def test_save_commits_master_and_preserves_body_metadata_and_fixed_readers(published, monkeypatch):
    from ultrakb.storage import wiki_projection

    root, identity, store, first, _ = published
    scope = storage_scope(root, identity)
    opened = read_page(root, "concepts/one", scope=scope)

    def fail_projection(*args, **kwargs):
        raise OSError("injected projection failure")

    body = "---\nA literal body opening\n---\n[[concepts/two]] [[concepts/missing]]\n"
    with monkeypatch.context() as fault:
        fault.setattr(wiki_projection, "atomic_write_bytes", fail_projection)
        saved = save_page(root, "concepts/one", body, version=opened.version, scope=scope)
    assert saved.status == "saved"
    assert saved.projection.status == "pending"
    assert saved.draft == body
    assert saved.ghosts_stripped == ("concepts/missing",)
    assert saved.page.body == body.replace("[[concepts/missing]]", "missing")
    assert saved.page.content.startswith("---\ntype: Concept\ndescription: Original\n---\n---\n")
    assert saved.page.publication_id != first.publication_id
    assert read_page(root, "concepts/one", scope=scope) == opened
    current = storage_scope(root, identity)
    assert read_page(root, "concepts/one", scope=current) == saved.page
    assert store.bind().read("sources/image.png") == b"retained image"
    assert (root / "wiki/concepts/one.md").read_text().endswith("One\n")
    assert ProjectionStore(root, identity).sync().status == "synced"
    assert (root / "wiki/concepts/one.md").read_text() == saved.page.content


@pytest.mark.parametrize("wrong", ["publication", "version"])
def test_save_rejects_stale_confirmation_and_returns_the_current_page(published, wrong):
    root, identity, store, first, files = published
    scope = storage_scope(root, identity)
    opened = read_page(root, "concepts/one", scope=scope)
    if wrong == "publication":
        files["concepts/two.md"] = b"Concurrent unrelated change"
        store.publish(store.prepare_revision(files, expected=first.publication_id))
    result = save_page(
        root,
        "concepts/one",
        "Keep my draft",
        version="wrong" if wrong == "version" else opened.version,
        scope=scope,
    )
    assert result.status == "conflict"
    assert result.draft == "Keep my draft"
    assert result.page == read_page(root, "concepts/one", scope=storage_scope(root, identity))
    assert result.projection is None
    assert store.bind().read("concepts/one.md") == files["concepts/one.md"]


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_final_publication_rechecks_a_racing_application_edit(published, monkeypatch, operation):
    root, identity, store, first, files = published
    scope = storage_scope(root, identity)
    opened = read_page(root, "concepts/one", scope=scope)
    native_publish = KnowledgeStore.publish
    concurrent = None

    def racing_publish(self, prepared, **kwargs):
        nonlocal concurrent
        if concurrent is None:
            changed = {**files, "concepts/one.md": b"Concurrent accepted body"}
            competing = self.prepare_revision(changed, expected=first.publication_id)
            concurrent = native_publish(self, competing)
        return native_publish(self, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "publish", racing_publish)
    if operation == "save":
        result = save_page(root, "concepts/one", "My draft", version=opened.version, scope=scope)
        assert result.status == "conflict"
        assert result.draft == "My draft"
        assert result.page.body == "Concurrent accepted body"
        assert result.page.publication_id == concurrent.publication_id
        assert result.candidate_revision_id is not None
    else:
        result = delete_page(root, "concepts/one", version=opened.version, scope=scope)
        assert result["status"] == "conflict"
        assert result["candidate_revision_id"] is not None
    assert store.bind().publication_id == concurrent.publication_id


def test_deletion_uses_master_backlinks_and_reports_projection_failure(published, monkeypatch):
    from ultrakb.storage import wiki_projection

    root, identity, store, first, files = published
    scope = storage_scope(root, identity)
    (root / "wiki/concepts/two.md").write_text("Unpublished draft without a link")
    preview = delete_page(root, "concepts/one", dry_run=True, scope=scope)
    assert preview["status"] == "dry_run"
    assert preview["backlinks"] == ["concepts/two"]
    assert preview["publication_id"] == first.publication_id

    def fail_projection(*args, **kwargs):
        raise OSError("injected projection failure")

    with monkeypatch.context() as fault:
        fault.setattr(wiki_projection, "durable_unlink", fail_projection)
        deleted = delete_page(
            root,
            "concepts/one",
            version=preview["version"],
            publication_id=preview["publication_id"],
            scope=scope,
        )
    assert deleted["status"] == "deleted"
    assert deleted["projection"].status == "pending"
    assert deleted["backlinks"] == ["concepts/two"]
    bound = store.bind()
    with pytest.raises(FileNotFoundError):
        bound.read("concepts/one.md")
    assert bound.read("concepts/two.md") == b"See one and untouched ghost.\n"
    assert bound.read("concepts/other.md") == files["concepts/other.md"]
    assert bound.read("index.md") == b"# Index\n## Concepts\n- [[concepts/two]]: See one\n"
    assert read_page(root, "concepts/one", scope=scope).body == "One\n"
    assert (root / "wiki/concepts/two.md").read_text() == "Unpublished draft without a link"


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_stale_delete_or_save_cannot_replace_a_new_public_head(published, operation):
    root, identity, store, first, files = published
    old = storage_scope(root, identity)
    preview = delete_page(root, "concepts/one", dry_run=True, scope=old)
    files["concepts/two.md"] = b"New unrelated content"
    current = store.publish(store.prepare_revision(files, expected=first.publication_id))
    if operation == "save":
        result = save_page(root, "concepts/one", "Draft", scope=old)
        assert result.status == "conflict"
    else:
        result = delete_page(
            root,
            "concepts/one",
            version=preview["version"],
            publication_id=preview["publication_id"],
            scope=old,
        )
        assert result["status"] == "conflict"
    assert store.bind().publication_id == current.publication_id
    assert store.bind().read("concepts/one.md") == files["concepts/one.md"]


def test_manual_save_publishes_during_external_pause_without_replacing_the_draft(published):
    from ultrakb.application.wiki_worktree import begin_external_edit

    root, identity, store, first, _ = published
    scope = storage_scope(root, identity)
    edit = begin_external_edit(root, identity)
    draft = root / "wiki/concepts/one.md"
    draft.write_text("Unaccepted external draft")
    saved = save_page(root, "concepts/one", "Application edit", scope=scope)
    assert saved.status == "saved"
    assert saved.projection.status == "paused"
    assert saved.page.publication_id != first.publication_id
    assert draft.read_text() == "Unaccepted external draft"
    assert begin_external_edit(root, identity).edit_id == edit.edit_id
    assert store.bind().read("concepts/one.md").endswith(b"Application edit")


def test_revision_patch_reuses_immutable_assets_without_reading_them(published, monkeypatch):
    from ultrakb.storage.blobs import blob_path

    root, identity, store, first, files = published
    bound = store.bind()
    image_digest = bound.files()["sources/image.png"]
    image = blob_path(root, image_digest)
    native_open = Path.open

    def measured_open(path, *args, **kwargs):
        if path == image:
            raise AssertionError("An unchanged asset must reuse its verified immutable manifest")
        return native_open(path, *args, **kwargs)

    with monkeypatch.context() as measured:
        measured.setattr(Path, "open", measured_open)
        prepared = store.prepare_changes(bound, {"concepts/one.md": b"Patched"})
        store.publish(prepared)
    assert store.bind().read("sources/image.png") == files["sources/image.png"]
    assert store.bind().read("concepts/one.md") == b"Patched"
    assert bound.publication_id == first.publication_id


def test_manual_edit_keeps_a_generated_pages_original_processing_basis(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    store = KnowledgeStore(root, identity)
    original = tmp_path / "original.md"
    previous = None
    sources, units = [], []
    for ordinal, text in enumerate(("Original evidence", "Replacement evidence")):
        original.write_text(text)
        source = catalog.admit(original, source_key="stable-source")
        processed = catalog.retain_processing(
            source.revision_id,
            {"body.md": original},
            body_path="body.md",
            fingerprint="policy",
            operation_id=f"operation-{ordinal}",
            decision=classify_markdown_tokens(2),
        )
        unit = catalog.plan_unit(processed.processing_id, key="body", doc_name="original")
        publication = store.publish(
            store.prepare_revision(
                {"concepts/one.md": b"---\nsources: [original]\n---\nGenerated claim"},
                expected=previous,
                unit_revisions=(unit.unit_revision_id,),
            )
        )
        previous = publication.publication_id
        sources.append(source)
        units.append(unit)
    scope = storage_scope(root, identity)
    saved = save_page(root, "concepts/one", "Manually refined claim", scope=scope)
    assert saved.status == "saved"
    assert saved.page.source_revision_ids == (sources[0].revision_id,)
    current = store.bind()
    assert current.unit_revisions() == (units[1].unit_revision_id,)
    assert current.page_input_revisions()["concepts/one.md"] == (units[0].unit_revision_id,)
    assert current.read_source(units[0].unit_revision_id, "body.md") == b"Original evidence"
