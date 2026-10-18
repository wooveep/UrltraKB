"""P0 publication witnesses bind immutable content rather than mutable Wiki files."""


def test_public_read_keeps_one_version_and_conflicting_writer_cannot_replace_it(tmp_path):
    import pytest

    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict

    root = tmp_path / "kb"
    identity = create_database(root)
    store = KnowledgeStore(root, identity)
    first = store.prepare_revision(
        {"index.md": b"Old directory", "sources/image.png": b"old image"}, expected=None
    )
    published = store.publish(first)
    bound = store.bind()
    next_revision = store.prepare_revision(
        {"index.md": b"New directory", "sources/image.png": b"new image"},
        expected=published.publication_id,
    )
    competing = store.prepare_revision(
        {"index.md": b"Conflicting directory"}, expected=published.publication_id
    )
    current = store.publish(next_revision)
    with pytest.raises(PublicationConflict):
        store.publish(competing)
    assert store.bind().publication_id == current.publication_id
    assert bound.read("index.md") == b"Old directory"
    assert bound.read("sources/image.png") == b"old image"
    assert store.bind().read("index.md") == b"New directory"
