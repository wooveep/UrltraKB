"""Real ConDB ownership and tree identity in a shared SQLite database."""

import sqlite3

import pytest


def test_borrowed_condb_preserves_connection_ownership_and_tree_local_entities(tmp_path):
    from contextdb import TreeDB

    connection = sqlite3.connect(tmp_path / "shared.sqlite", isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    TreeDB.initialize_schema(connection)
    connection.execute("COMMIT")
    original_factory = connection.row_factory
    queries = []
    connection.set_trace_callback(queries.append)
    storage = TreeDB(connection=connection)
    assert connection.row_factory is original_factory
    assert not any("PRAGMA" in query.upper() for query in queries)
    with pytest.raises(RuntimeError, match="caller transaction"):
        storage.create_tree()
    connection.execute("BEGIN IMMEDIATE")
    root = {"node_id": "same-node", "type": "leaf", "entity_id": "same-entity"}
    first = storage.ingest_tree(root, {"same-entity": {"type": "content", "text": "first"}})
    second = storage.ingest_tree(root, {"same-entity": {"type": "content", "text": "second"}})
    storage.close()
    assert connection.in_transaction
    connection.execute("COMMIT")
    reader = TreeDB(connection=connection, read_only=True)
    assert '"first"' in reader.get_entity(first, "same-node").payload_json
    assert '"second"' in reader.get_entity(second, "same-node").payload_json
    reader.close()
    assert connection.execute("SELECT count(*) FROM trees").fetchone()[0] == 2
    connection.close()


def test_shared_ready_trees_keep_large_content_and_same_entities_isolated(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = TreeStore(root, identity)
    first_content = "original body " * 9000
    first = store.build({"content": first_content}, protocol="generic", input_fingerprint="a" * 64)
    second = store.build(
        {"content": "another body"}, protocol="generic", input_fingerprint="b" * 64
    )
    for tree, content in ((first, first_content), (second, "another body")):
        with store.open(tree.tree_id) as bound:
            assert bound.content(bound.root_id)["content"] == content
    assert not list((root / ".ultrakb").glob("**/context.sqlite"))


def test_ready_document_index_is_shared_by_publications_and_retains_full_source(tmp_path):
    from test_condb_storage import document

    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.index_store import IndexStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    original = tmp_path / "input.md"
    original.write_text("Original source text")
    catalog = CatalogStore(root, identity)
    revision = catalog.admit(original, source_key="original")
    processed = catalog.retain_processing(
        revision.revision_id,
        {"body.md": original},
        body_path="body.md",
        fingerprint="plain-v1",
        operation_id="plain-original",
        decision=classify_markdown_tokens(4),
    )
    unit = catalog.plan_unit(processed.processing_id, key="main", doc_name="original")
    store = IndexStore(root, identity)
    source = document()
    accepted = store.accept(unit.unit_revision_id, source, content_fingerprint="a" * 64)
    original.unlink()
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"first"},
            expected=None,
            index_revisions=(accepted.index_revision_id,),
        )
    )
    second = knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"second"},
            expected=first.publication_id,
            index_revisions=(accepted.index_revision_id,),
        )
    )
    assert store.document(accepted.index_revision_id) == source
    assert knowledge.bind(first.publication_id).index_revisions() == (accepted.index_revision_id,)
    assert knowledge.bind(second.publication_id).index_revisions() == (accepted.index_revision_id,)
    assert store.find(unit.unit_revision_id, content_fingerprint="a" * 64) == accepted
    from ultrakb.storage.maintenance import Maintenance

    assert Maintenance(root, identity).preview().protected_trees == 1
    Maintenance(root, identity).collect()
    assert store.document(accepted.index_revision_id) == source


def test_tree_reclamation_is_busy_during_real_reads_and_deletes_only_one_tree(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    trees, maintenance = TreeStore(root, identity), Maintenance(root, identity)
    first = trees.build({"content": "first"}, protocol="generic", input_fingerprint="a" * 64)
    second = trees.build({"content": "second"}, protocol="generic", input_fingerprint="b" * 64)
    assert maintenance.preview().candidate_trees == 2
    with trees.open(first.tree_id) as selected:
        assert maintenance.collect().status == "busy"
        assert selected.content(selected.root_id)["content"] == "first"
    result = maintenance.collect(max_items=1)
    assert result.trees_deleted == 1
    readable = []
    for revision in (first, second):
        try:
            with trees.open(revision.tree_id) as selected:
                readable.append(selected.content(selected.root_id)["content"])
        except ValueError as error:
            assert "ready" in str(error)
    assert len(readable) == 1
    assert readable[0] in {"first", "second"}


def test_large_tree_retirement_resumes_in_bounded_batches(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = TreeStore(root, identity)
    revision = store.build(
        {"children": {str(i): {"content": f"retained leaf {i}"} for i in range(160)}},
        protocol="generic",
        input_fingerprint="a" * 64,
    )
    batches = []
    for _ in range(10):
        batch = Maintenance(root, identity).collect(max_items=1)
        batches.append(batch)
        assert batch.tree_rows_deleted <= 128
        if batch.trees_deleted:
            break
    assert len(batches) > 1
    assert sum(batch.trees_deleted for batch in batches) == 1
    assert sum(batch.tree_rows_deleted for batch in batches) == 321
    with pytest.raises(ValueError, match="ready"):
        with store.open(revision.tree_id):
            pytest.fail("Retired tree became readable")


def test_real_sdk_block_query_reads_borrowed_tree_and_large_cas_entities(tmp_path):
    import json
    import re

    from ultrakb.storage.database import create_database
    from ultrakb.storage.tree_store import TreeStore

    class RankResponses:
        max_concurrent = 4

        def chat(self, messages, **options):
            aliases = list(dict.fromkeys(re.findall(r"\bn\d+\b", json.dumps(messages))))
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "rank",
                        "id": "rank",
                        "input": {"ranked_ids": aliases, "done": False},
                    }
                ]
            }

        def chat_with_cache(self, messages, **options):
            return self.chat(
                [
                    *messages,
                    {"content": options.get("cache_content", [])},
                    {"content": options.get("non_cached_content", "")},
                ]
            )

    root = tmp_path / "kb"
    store = TreeStore(root, create_database(root))
    text = "Original source text for complete retrieval. " * 600
    revision = store.build(
        {
            "title": "Knowledge",
            "is_dir": True,
            "children": {
                str(group): {
                    "title": f"Group {group}",
                    "is_dir": True,
                    "children": {
                        str(i): {
                            "title": f"Document {group}-{i}",
                            "rel_path": f"Document {group}-{i}",
                            "is_dir": False,
                            "content": text,
                            "source_ref": {"path": f"sources/document-{group}-{i}.md"},
                        }
                        for i in range(4)
                    },
                }
                for group in range(3)
            },
        },
        protocol="generic",
        input_fingerprint="c" * 64,
        meta={"source": "filesystem"},
    )
    with store.open(revision.tree_id) as bound:
        result = bound.query(
            "original source text",
            strategy="block",
            llm=RankResponses(),
            max_tokens_per_block=1000,
            max_parallel_blocks=4,
            select_k=3,
            max_turns=4,
        )
        assert result.contents
        assert all(item["content"]["content"] == text for item in result.contents)


@pytest.mark.parametrize("arguments", [{"data": ["discarded"]}, {"meta": ["invalid"]}])
def test_tree_build_rejects_invalid_input_shapes_before_persistence(tmp_path, arguments):
    from contextdb import ConDB

    from ultrakb.storage.database import create_database
    from ultrakb.storage.tree_store import TreeStore

    arguments = {"data": {"content": "valid"}, "meta": None, **arguments}
    with pytest.raises(ValueError, match="object"):
        ConDB.prepare(**arguments, format="generic")
    root = tmp_path / "kb"
    store = TreeStore(root, create_database(root))
    with pytest.raises(ValueError, match="object"):
        store.build(**arguments, protocol="generic", input_fingerprint="a" * 64)


def test_borrowed_condb_supports_the_hosts_named_row_factory(tmp_path):
    from contextdb import TreeDB

    connection = sqlite3.connect(tmp_path / "host.sqlite", isolation_level=None)
    connection.execute("BEGIN")
    TreeDB.initialize_schema(connection)
    connection.execute("COMMIT")

    def factory(cursor, row):
        return {column[0]: value for column, value in zip(cursor.description, row)}

    connection.row_factory = factory
    with TreeDB(connection=connection) as storage:
        storage.check_integrity()
        with pytest.raises(RuntimeError, match="caller transaction"):
            storage.create_tree()
        connection.execute("BEGIN")
        tree, node = storage.create_tree()
        connection.execute("COMMIT")
        assert storage.get_node(tree, node).node_id == node
    assert connection.row_factory is factory
    assert connection.execute("SELECT count(*) AS count FROM trees").fetchone() == {"count": 1}
    connection.close()


def test_large_document_pages_and_navigation_extensions_remain_lossless(tmp_path):
    from tests.test_condb_storage import document
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = TreeStore(root, identity)
    source = document()
    source["pages"] = [{"page": 1, "text": "Original page text. " * 9000}]
    source["structure"][0]["extension"] = {"annotation": "Large source extension. " * 9000}
    revision = store.build(source, protocol="document", input_fingerprint="c" * 64)
    with store.open(revision.tree_id) as bound:
        assert bound.content(bound.root_id)["document"] == source
        first = bound.children(bound.root_id)[0]
        assert first["attrs"]["extension"] == source["structure"][0]["extension"]
        assert bound.content(first["node_id"])["source"] == source["structure"][0]
    maintenance = Maintenance(root, identity)
    assert maintenance.collect(max_items=1).trees_deleted == 1
    for _ in range(10):
        if maintenance.preview().candidate_count == 0:
            break
        maintenance.collect()
    assert maintenance.preview().protected_count == 0


@pytest.mark.parametrize("table", ["nodes", "entities"])
def test_draft_rows_cannot_be_moved_into_a_ready_tree(tmp_path, table):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = TreeStore(root, identity)
    revision = store.build({"content": "ready"}, protocol="generic", input_fingerprint="a" * 64)
    with store.open(revision.tree_id) as bound:
        root_id = bound.root_id
    with storage_stage(root, identity) as db:
        with db.transaction() as connection:
            connection.execute("INSERT INTO trees VALUES ('draft', 'draft-child', 1, 1, NULL)")
            connection.execute(
                "INSERT INTO ukb_tree_builds VALUES ('draft', 'generic', ?, 'draft-generation', "
                "?, 1, 1, 'draft')",
                ("b" * 64, revision.manifest_digest),
            )
            connection.execute(
                "INSERT INTO nodes VALUES ('draft', 'draft-child', NULL, NULL, 2, 0, '/r', "
                "NULL, NULL, NULL, 1, 1)"
            )
            connection.execute(
                "INSERT INTO entities VALUES ('draft', 'draft-entity', 'content', '{}', 1)"
            )
        with (
            pytest.raises(sqlite3.IntegrityError, match="immutable"),
            db.transaction() as connection,
        ):
            if table == "nodes":
                connection.execute(
                    "UPDATE nodes SET tree_id = ?, parent_id = ?, depth = 1, path = '/r/child' "
                    "WHERE tree_id = 'draft'",
                    (revision.tree_id, root_id),
                )
            else:
                connection.execute(
                    "UPDATE entities SET tree_id = ? WHERE tree_id = 'draft'", (revision.tree_id,)
                )
