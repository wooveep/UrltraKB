"""Candidate publications carry complete deterministic navigation before activation."""

import json

import pytest


def test_empty_publication_has_ready_navigation_and_reads_do_not_write(tmp_path):
    from ultrakb.storage.database import create_database, database_path
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    store = KnowledgeStore(root, create_database(root))
    published = store.publish(store.prepare_revision({}, expected=None))
    path = database_path(root)
    before = (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes())
    for _ in range(2):
        bound = store.bind(published.publication_id)
        with bound.navigation() as navigation:
            assert navigation.children(navigation.root_id) == []
    assert (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes()) == before


def test_navigation_binds_exact_retained_pages_and_can_be_explicitly_repaired(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.index_store import IndexStore
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.navigation_store import RecoveryRequired

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog, knowledge = CatalogStore(root, identity), KnowledgeStore(root, identity)
    original = tmp_path / "original.pdf"
    original.write_bytes(b"Managed original document")
    revision = catalog.admit(original, source_key="original")
    body, pages = tmp_path / "body.md", tmp_path / "pages.json"
    body.write_text("original source body")
    page_content = [{"page": 1, "content": "original source body", "unit_kind": "page"}]
    pages.write_text(json.dumps(page_content))
    processing = catalog.retain_processing(
        revision.revision_id,
        {"body.md": body, "pages.json": pages},
        body_path="body.md",
        source_path="pages.json",
        source_kind="pages",
        fingerprint="pdf-pages-v1",
        operation_id="original-pages",
        decision=classify_markdown_tokens(100),
    )
    unit = catalog.plan_unit(processing.processing_id, key="main", doc_name="original")
    document = {
        "doc_name": "original",
        "pages": page_content,
        "structure": [{"node_id": "n1", "title": "First page", "start_index": 1, "end_index": 1}],
    }
    index = IndexStore(root, identity).accept(
        unit.unit_revision_id, document, content_fingerprint="a" * 64
    )
    invalid = {
        **document,
        "structure": [
            {"node_id": "n1", "title": "Missing page", "start_index": 99, "end_index": 99},
        ],
    }
    with pytest.raises(ValueError, match="native source"):
        IndexStore(root, identity).accept(
            unit.unit_revision_id, invalid, content_fingerprint="b" * 64
        )
    candidate = knowledge.prepare_revision(
        {"index.md": b"complete wiki"},
        expected=None,
        index_revisions=(index.index_revision_id,),
    )
    published = knowledge.publish(candidate)
    original.unlink()
    body.unlink()
    pages.unlink()
    bound = knowledge.bind(published.publication_id)
    with bound.navigation() as navigation:
        first = navigation.children(navigation.root_id)[0]
        reference = first["attrs"]["source_ref"]
        assert reference["unit_revision_id"] == unit.unit_revision_id
        assert reference["source_revision_id"] == revision.revision_id
        assert reference["knowledge_revision_id"] == published.revision_id
        assert reference["native_location"] == {"pages": "1-1"}
        tree_id = navigation.revision.tree_id
    # Simulate an interrupted/corrupted navigation binding on a disposable store.
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "DELETE FROM ukb_revision_navigation WHERE revision_id = ?", (published.revision_id,)
        )
        connection.execute(
            "DELETE FROM ukb_tree_refs WHERE owner_kind = 'knowledge-revision' AND owner_id = ?",
            (published.revision_id,),
        )
    with pytest.raises(RecoveryRequired, match="navigation"):
        with bound.navigation():
            pytest.fail("A read silently rebuilt missing navigation")
    knowledge.repair_navigation(published.publication_id)
    with bound.navigation() as repaired:
        assert repaired.revision.tree_id != tree_id
        assert repaired.children(repaired.root_id)[0]["attrs"]["source_ref"] == reference


def test_short_source_navigation_keeps_each_publications_exact_processing_input(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, original = tmp_path / "kb", tmp_path / "source.md"
    identity = create_database(root)
    catalog, knowledge = CatalogStore(root, identity), KnowledgeStore(root, identity)
    publications, expected = [], None
    for ordinal, text in enumerate(("first original", "second source is longer")):
        original.write_text(text)
        source = catalog.admit(original, source_key="stable-source")
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": original},
            body_path="body.md",
            fingerprint="markdown-v1",
            operation_id=f"process-{ordinal}",
            decision=classify_markdown_tokens(10),
        )
        unit = catalog.plan_unit(processing.processing_id, key="main", doc_name="source")
        published = knowledge.publish(
            knowledge.prepare_revision(
                {"index.md": text.encode()},
                expected=expected,
                unit_revisions=(unit.unit_revision_id,),
            )
        )
        publications.append((published, processing, text))
        expected = published.publication_id
    original.unlink()
    for published, processing, text in publications:
        with knowledge.bind(published.publication_id).navigation() as tree:
            leaf = tree.children(tree.root_id)[0]
            reference = leaf["attrs"]["source_ref"]
            assert reference["processing_id"] == processing.processing_id
            assert reference["native_location"] == {"chars": f"0:{len(text)}"}
            assert (
                catalog.read_processed(reference["processing_id"], reference["path"])
                == text.encode()
            )
