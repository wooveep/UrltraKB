"""ConDB's public storage boundary: lossless documents and composable SQLite."""

import copy
import json
import shutil
import sqlite3

import pytest
from contextdb import ConDB, ContextTree, TreeDB
from contextdb.adapter import DocumentTreeAdapter


def document():
    return {
        "doc_id": "original-doc",
        "doc_name": "Mixed source",
        "metadata": {"source_type": "mixed", "extra": ["来源", 3]},
        "custom": {"future": True},
        "structure": [
            {
                "node_id": f"n{i}",
                "title": f"Section {i}",
                "text": "全文🙂" * 90,
                "source_locator": locator,
                "extension": {"order": i},
            }
            for i, locator in enumerate(
                [
                    {"kind": "pdf", "physical_page": 3},
                    {"kind": "block", "block_id": "B1", "start_char": 1, "end_char": 6},
                    {"kind": "slide", "slide_number": 2, "part": "body"},
                    {"kind": "slide", "slide_number": 2, "part": "notes"},
                    {"kind": "worksheet", "sheet": "Sheet1", "cell_range": "A1:C7"},
                ]
            )
        ],
    }


@pytest.mark.parametrize("api", ["condb", "context_tree"])
def test_document_roundtrip_keeps_source_identity_locators_and_extensions(
    tmp_path, monkeypatch, api
):
    import litellm

    monkeypatch.setattr(
        litellm, "completion", lambda *a, **kw: pytest.fail("Unexpected model call")
    )
    source = document()
    db = TreeDB(str(tmp_path / "context.sqlite"))
    client = ConDB(storage=db) if api == "condb" else ContextTree(storage=db)
    tree_id = client.store(source) if api == "condb" else client.index_document_tree(source)
    root_id = db.get_root_id(tree_id)
    assert client.get_content(tree_id, root_id)["document"] == source
    children = db.get_children(tree_id, root_id)
    assert [n.node_id for n in children] == [n["node_id"] for n in source["structure"]]
    for node, original in zip(children, source["structure"]):
        assert client.get_content(tree_id, node.node_id)["source"] == original
        assert json.loads(node.attrs_json)["source_locator"] == original["source_locator"]
        assert "page_start" not in json.loads(node.attrs_json)
    client.close()
    assert db.get_root_id(tree_id) == root_id  # Injected storage is owned by the caller.
    db.delete_tree(tree_id)
    assert db.get_root_id(tree_id) is None
    db.close()


def test_outer_transaction_and_nested_savepoint_roll_back_tree_and_mapping(tmp_path):
    db = TreeDB(str(tmp_path / "context.sqlite"))
    client = ConDB(storage=db)
    db.conn.execute("CREATE TABLE business_reference (name TEXT, tree_id TEXT)")
    with pytest.raises(RuntimeError):
        with db.transaction():
            doomed = client.store(document())
            db.conn.execute("INSERT INTO business_reference VALUES (?, ?)", ("checkpoint", doomed))
            raise RuntimeError("abort publication")
    assert db.get_root_id(doomed) is None
    assert db.conn.execute("SELECT * FROM business_reference").fetchall() == []
    with db.transaction():
        kept = client.store(document())
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.delete_tree(kept)
                raise RuntimeError("abort inner savepoint")
        assert db.get_root_id(kept)
        db.conn.execute("INSERT INTO business_reference VALUES (?, ?)", ("active", kept))
    # A transaction opened by a caller on the native connection composes too.
    db.conn.execute("BEGIN")
    created, _ = db.create_tree()
    db.delete_tree(kept)
    db.conn.rollback()
    assert db.get_root_id(created) is None and db.get_root_id(kept)
    assert db.conn.execute("SELECT tree_id FROM business_reference").fetchone()[0] == kept
    db.close()


def test_sealed_database_copies_as_one_file_and_read_only_never_initializes(tmp_path):
    path = tmp_path / "writable.sqlite"
    db = TreeDB(str(path))
    source = document()
    source["structure"] = [
        dict(source["structure"][0], node_id=f"n{i}", title=str(i)) for i in range(14)
    ]
    tree_id = ConDB(storage=db).store(source)
    root = db.get_root_id(tree_id)
    assert [n.node_id for n in db.get_children(tree_id, root)] == [f"n{i}" for i in range(14)]
    assert [n["node_id"] for n in db.get_subtree(tree_id, root)][1:] == [f"n{i}" for i in range(14)]
    db.checkpoint()
    db.close()
    copy_dir = tmp_path / "published"
    copy_dir.mkdir()
    target = copy_dir / "context.sqlite"
    shutil.copy2(path, target)
    target.chmod(0o444)
    copy_dir.chmod(0o555)
    before = target.read_bytes()
    try:
        with TreeDB(str(target), read_only=True) as sealed:
            assert ConDB(storage=sealed).get_content(tree_id, root)["document"] == source
            with pytest.raises(PermissionError):
                sealed.delete_tree(tree_id)
        assert target.read_bytes() == before
        assert [p.name for p in copy_dir.iterdir()] == ["context.sqlite"]
    finally:
        copy_dir.chmod(0o755)
        target.chmod(0o644)
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        TreeDB(str(missing), read_only=True)
    assert not missing.exists()
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    before = target.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        TreeDB(str(target), read_only=True)
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["source", "identity", "duplicate", "locator", "order", "nonjson"]
)
def test_document_contract_rejects_incomplete_or_ambiguous_source(damage):
    value = copy.deepcopy(document())
    if damage == "source":
        del value["doc_name"]
    elif damage == "identity":
        del value["structure"][0]["node_id"]
    elif damage == "duplicate":
        value["structure"][1]["node_id"] = "n0"
    elif damage == "locator":
        del value["structure"][0]["source_locator"]
    elif damage == "order":
        value["structure"] = {}
    else:
        value["custom"] = float("nan")
    with pytest.raises(ValueError):
        DocumentTreeAdapter().convert(value)


def test_failed_ingestion_preserves_outer_transaction_and_removes_partial_entities(tmp_path):
    with TreeDB(str(tmp_path / "context.sqlite")) as db:
        with db.transaction():
            kept, _ = db.create_tree()
            with pytest.raises(sqlite3.IntegrityError):
                db.ingest_tree(
                    {"node_id": "same", "children": [{"node_id": "same"}]},
                    entities={"aborted": {"type": "text", "text": "must roll back"}},
                )
            assert db.get_root_id(kept)
            assert (
                db.conn.execute("SELECT * FROM entities WHERE entity_id='aborted'").fetchall() == []
            )
        assert db.conn.execute("SELECT count(*) FROM trees").fetchone()[0] == 1
        with db.transaction(), pytest.raises(RuntimeError, match="active transaction"):
            db.checkpoint()


def test_readonly_rejects_uncheckpointed_wal_and_malformed_schema(tmp_path):
    path = tmp_path / "context.sqlite"
    db = TreeDB(str(path))
    db.create_tree()
    with pytest.raises(ValueError, match="checkpointed"):
        TreeDB(str(path), read_only=True)
    db.checkpoint()
    db.conn.execute("DROP TABLE nodes")
    db.checkpoint()
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        TreeDB(str(path), read_only=True)
    assert path.read_bytes() == before


def test_reused_entity_identity_cannot_silently_replace_different_content(tmp_path):
    with TreeDB(str(tmp_path / "context.sqlite")) as db:
        tree = {"node_id": "root", "entity_id": "stable"}
        original = {"type": "text", "text": "original"}
        kept = db.ingest_tree(tree, {"stable": original})
        with pytest.raises(ValueError, match="conflicting entity"):
            db.ingest_tree(tree, {"stable": {"type": "text", "text": "changed"}})
        assert json.loads(db.get_entity(kept, "root").payload_json) == original


@pytest.mark.parametrize("extension", [{1: "numeric", "1": "text"}, {"tuple": (1, 2)}])
def test_document_rejects_extensions_that_json_would_silently_change(extension):
    value = document()
    value["metadata"] = extension
    with pytest.raises(ValueError, match="JSON"):
        DocumentTreeAdapter().convert(value)


def test_reopening_preserves_foreign_key_contract_for_business_references(tmp_path):
    path = tmp_path / "context.sqlite"
    with TreeDB(str(path)) as db:
        db.conn.execute(
            "CREATE TABLE business (tree_id TEXT REFERENCES trees(tree_id) ON DELETE CASCADE)"
        )
        with db.transaction():
            tree, _ = db.create_tree()
            db.conn.execute("INSERT INTO business VALUES (?)", (tree,))
    with TreeDB(str(path)) as reopened:
        reopened.delete_tree(tree)
        assert reopened.conn.execute("SELECT * FROM business").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError), reopened.transaction():
            reopened.conn.execute("INSERT INTO business VALUES ('missing')")
