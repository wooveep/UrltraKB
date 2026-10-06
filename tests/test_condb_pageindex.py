"""PageIndex StorageEngine through ConDB and portable managed inputs."""

import hashlib
import json
import shutil
import sqlite3

import pytest
from pageindex.errors import CollectionAlreadyExistsError
from pageindex.storage.protocol import StorageEngine


def indexed_document(root, *, name="source", key="policy-key", identity="document"):
    path = root / "default" / f"{identity}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"retained input")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "doc_name": name,
        "doc_description": "description",
        "doc_type": "pdf",
        "file_path": str(path),
        "file_hash": key,
        "metadata": {
            "source_digest": digest,
            "processing_fingerprint": key,
            "unit_kind": "page",
            "unit_count": 1,
            "parser_policy": "physical-pdf-v1",
            "sdk_version": "test",
        },
        "structure": [
            {
                "node_id": "original-node",
                "title": "Title",
                "start_index": 1,
                "end_index": 1,
                "text": "全文🙂",
                "extension": {"field": [1, 2]},
            }
        ],
        "pages": [{"page": 1, "content": "全文🙂"}],
        "extension": {"unknown": "retained"},
    }


def test_storage_contract_roundtrip_dedup_collections_and_relocation(tmp_path):
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation

    package = tmp_path / "staging"
    inputs = package / "separate-inputs"
    location = IndexLocation(package / "db/context.sqlite", inputs)
    doc = indexed_document(inputs)
    with ConDBPageIndexStorage(location) as storage:
        assert isinstance(storage, StorageEngine)
        storage.create_collection("default")
        storage.get_or_create_collection("default")
        with pytest.raises(CollectionAlreadyExistsError):
            storage.create_collection("default")
        storage.save_document("default", "document", doc)
        assert storage.find_document_by_hash("default", "policy-key") == "document"
        assert storage.find_document_by_hash("default", doc["metadata"]["source_digest"]) is None
        loaded = storage.get_document("default", "document")
        assert loaded["metadata"] == doc["metadata"]
        assert loaded["extension"] == doc["extension"]
        assert loaded["doc_id"] == "document"
        assert storage.get_document_structure("default", "document") == doc["structure"]
        assert storage.get_pages("default", "document") == doc["pages"]
        assert storage.list_documents("default")[0]["doc_id"] == "document"
        with pytest.raises(sqlite3.IntegrityError):
            storage.save_document(
                "default", "duplicate", indexed_document(inputs, identity="duplicate")
            )
        assert len(storage.list_documents("default")) == 1
    moved = tmp_path / "published"
    shutil.copytree(package, moved)
    shutil.rmtree(package)
    with ConDBPageIndexStorage(
        IndexLocation(moved / "db/context.sqlite", moved / "separate-inputs", read_only=True)
    ) as storage:
        loaded = storage.get_document("default", "document")
        assert loaded["file_path"] == str(moved / "separate-inputs/default/document.pdf")
        assert storage.get_pages("default", "document") == doc["pages"]
        assert storage.list_collections() == ["default"]
        with pytest.raises(PermissionError):
            storage.delete_document("default", "document")
    with ConDBPageIndexStorage(
        IndexLocation(moved / "db/context.sqlite", moved / "separate-inputs")
    ) as storage:
        storage.delete_document("default", "document")
        assert storage.get_document("default", "document") == {}
        storage.delete_collection("default")
        assert storage.list_collections() == []


def test_real_client_is_portable_sealed_and_never_mutates_on_reads(tmp_path, monkeypatch):
    from pageindex import IndexConfig
    from test_index_llm import RecordingIndexLLM

    from openkb.index_client import create_index_client
    from openkb.index_location import IndexLocation
    from openkb.index_packages import seal_index_package

    source = tmp_path / "source.md"
    source.write_text("# 原标题\n完整原文🙂\n## 第二节\n更多正文")
    runtime = RecordingIndexLLM()
    package = tmp_path / "work"
    with create_index_client(
        storage_path=str(package),
        index_config=IndexConfig(llm_client=runtime, require_llm_client=True),
    ) as client:
        collection = client.collection()
        identity = collection.add(str(source))
        assert collection.add(str(source)) == identity
        original = collection.get_document(identity, include_text=True)
    assert (package / "context.sqlite").is_file()
    assert not (package / "pageindex.db").exists()
    seal_index_package(package, "revision-1")
    published = tmp_path / "revision/index"
    shutil.copytree(package, published)
    shutil.rmtree(package)
    location = IndexLocation.package(published)
    assert location.read_only and location.owner_revision == "revision-1"
    before = {p.relative_to(published): p.read_bytes() for p in published.rglob("*") if p.is_file()}
    monkeypatch.setattr(runtime, "complete", lambda *a, **kw: pytest.fail("Read attempted LLM"))
    with create_index_client(location=location) as reader:
        collection = reader.collection()
        loaded = collection.get_document(identity, include_text=True)
        assert loaded["structure"] == original["structure"]
        assert str(package) not in json.dumps(loaded)
        with pytest.raises(PermissionError):
            collection.add(str(source))
        with pytest.raises(PermissionError):
            collection.delete_document(identity)
    assert before == {
        p.relative_to(published): p.read_bytes() for p in published.rglob("*") if p.is_file()
    }


def test_mapping_failure_rolls_back_the_tree_and_complete_document(tmp_path):
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation

    with ConDBPageIndexStorage(IndexLocation.package(tmp_path / "index")) as storage:
        storage.create_collection("default")
        doc = indexed_document(storage.location.inputs_root)
        storage.db.conn.execute("""CREATE TRIGGER fail_mapping BEFORE INSERT ON okb_documents
            BEGIN SELECT RAISE(ABORT, 'injected mapping failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="injected mapping failure"):
            storage.save_document("default", "document", doc)
        assert storage.list_documents("default") == []
        assert storage.db.conn.execute("SELECT count(*) FROM trees").fetchone()[0] == 0
        assert storage.db.conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0


@pytest.mark.parametrize("collection,identity", [("default", "wrong"), ("wrong", "document")])
def test_document_identity_cannot_retarget_a_validated_input(tmp_path, collection, identity):
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation

    with ConDBPageIndexStorage(IndexLocation.package(tmp_path / "index")) as storage:
        storage.create_collection(collection)
        doc = indexed_document(storage.location.inputs_root)
        with pytest.raises(ValueError, match="document identity"):
            storage.save_document(collection, identity, doc)
        assert storage.list_documents(collection) == []
        assert storage.db.conn.execute("SELECT count(*) FROM trees").fetchone()[0] == 0
        assert storage.db.conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage", ["source", "range", "pages", "path", "missing_kind", "unknown_kind", "unit_count"]
)
def test_document_input_or_locator_corruption_is_rejected_before_commit(tmp_path, damage):
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation

    with ConDBPageIndexStorage(IndexLocation.package(tmp_path / "index")) as storage:
        storage.create_collection("default")
        doc = indexed_document(storage.location.inputs_root)
        if damage == "source":
            doc["metadata"]["source_digest"] = "0" * 64
        elif damage == "range":
            doc["structure"][0]["end_index"] = 2
        elif damage == "pages":
            doc["pages"] = []
        elif damage == "missing_kind":
            del doc["metadata"]["unit_kind"]
            doc["structure"][0]["end_index"] = 999
        elif damage == "unknown_kind":
            doc["metadata"]["unit_kind"] = "unknown"
        elif damage == "unit_count":
            doc["metadata"]["unit_count"] = True
        else:
            doc["file_path"] = str(tmp_path / "outside.pdf")
        with pytest.raises(ValueError):
            storage.save_document("default", "document", doc)
        assert storage.list_documents("default") == []


def test_dedup_failure_keeps_the_existing_document_and_managed_input(kb_dir, monkeypatch):
    from pathlib import Path

    from pageindex import IndexConfig
    from test_index_llm import RecordingIndexLLM

    from openkb import indexer
    from openkb.index_client import create_index_client
    from openkb.knowledge_scope import legacy_scope

    source = kb_dir / "input.md"
    source.write_text("# Title\nRetained original.")
    options = IndexConfig(llm_client=RecordingIndexLLM(), require_llm_client=True)
    monkeypatch.setattr(indexer, "_build_index_config", lambda *a, **kw: options)
    monkeypatch.setattr(
        indexer, "_convert_pdf_to_pages", lambda *a: [{"page": 1, "content": "Retained original."}]
    )
    result = indexer.index_long_document(source, kb_dir, scope=legacy_scope(kb_dir))

    def fail(*a, **kw):
        raise OSError("injected output failure")

    monkeypatch.setattr(indexer, "_write_long_doc_artifacts", fail)
    with pytest.raises(OSError, match="injected output failure"):
        indexer.index_long_document(source, kb_dir, scope=legacy_scope(kb_dir))
    with create_index_client(storage_path=kb_dir / ".openkb", index_config=options) as reader:
        document = reader.collection().get_document(result.doc_id, include_text=True)
        assert Path(document["file_path"]).is_file()
        assert "Retained original." in document["structure"][0]["text"]


@pytest.mark.parametrize("entry", ["context.sqlite", "files"])
def test_working_package_cannot_follow_an_external_storage_symlink(tmp_path, entry):
    from openkb.index_location import IndexLocation

    package = tmp_path / "index"
    package.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    sentinel = outside / "original"
    sentinel.write_bytes(b"preserved")
    try:
        (package / entry).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(ValueError):
        IndexLocation.package(package)
    assert sentinel.read_bytes() == b"preserved"
    assert list(outside.iterdir()) == [sentinel]
