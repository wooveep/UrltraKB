"""Immutable, fully validated document indexes independent of knowledge publication."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from pydantic import TypeAdapter

from ultrakb.file_state import contained_paths
from ultrakb.index_packages import copy_index_package
from ultrakb.locks import atomic_write_json
from ultrakb.mutation import _copy_file_atomic, mutation_scope
from ultrakb.source_catalog import read_source
from ultrakb.source_records import Digest, Record, RecordId, RelativePath
from ultrakb.state import HashRegistry
from ultrakb.unit_publication import copy_tree, wiki_versions


class IndexCheckpoint(Record):
    source_id: RecordId
    source_revision_id: RecordId
    unit_revision_id: RecordId
    identity: Digest
    document_id: str
    description: str
    tree: dict
    files: dict[RelativePath, Digest]


def checkpoint_root(kb_dir: Path, source_id: str) -> Path:
    TypeAdapter(RecordId).validate_python(source_id)
    root = kb_dir / ".ultrakb/index-checkpoints" / source_id
    contained_paths(kb_dir, [root])
    return root


def _validate_index(path, document_id):
    from ultrakb.condb_storage import ConDBPageIndexStorage
    from ultrakb.index_documents import validate_document
    from ultrakb.index_location import IndexLocation

    location = IndexLocation.package(path)
    if not location.database.is_file():
        raise ValueError("Index checkpoint is missing its document database")
    with ConDBPageIndexStorage(location) as storage:
        doc = storage.get_document("default", document_id)
        if not doc or not doc.get("structure"):
            raise ValueError("Index checkpoint has no complete document tree")
        doc["file_path"] = location.input_reference(doc["file_path"])
        doc["pages"] = storage.get_pages("default", document_id)
        validate_document(doc, location, "default", document_id)
    return doc["structure"]


def _navigation(nodes):
    # The SDK may materialize node text on read; source bytes are validated by
    # validate_document and the package digest, independently of this projection.
    return [
        {
            key: _navigation(value) if key == "nodes" else value
            for key, value in node.items()
            if key != "text"
        }
        for node in nodes
    ]


def _identities(normalized, index_input, executor):
    policy = json.loads(normalized.fingerprint)
    payload = {
        "source_revision": normalized.source_revision_id,
        "input": HashRegistry.hash_file(index_input),
        "policy": {
            key: policy.get(key)
            for key in ("index_model", "index_policy", "index_sdk", "index_endpoint")
        },
        "sdk": {name: version(name) for name in ("pageindex", "pageindex-condb")},
        "connection": executor.bindings.public_identity() if executor else None,
    }
    identities = [hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()]
    connection = payload["connection"]
    # Earlier receipts omitted generation options. Only absent options have the
    # same request meaning; every other policy and byte digest remains required.
    if connection is not None and connection.get("role_generation") == {}:
        payload["connection"] = {
            key: value for key, value in connection.items() if key != "role_generation"
        }
        identities.append(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
    # .14 changes request admission/recovery, not the frozen index content
    # contract. Existing .13 packages keep their paid, validated trees.
    if payload["sdk"]["pageindex"] == "0.3.0.dev3+urltrakb.14":
        payload["sdk"]["pageindex"] = "0.3.0.dev3+urltrakb.13"
        identities.append(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
        payload["connection"] = connection
        identities.append(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
    return tuple(identities)


def reusable_index(kb_dir, admission, revision, normalized, index_input, working, build):
    """Caller owns the KB lease; no knowledge head is captured in a pure index."""
    from ultrakb.indexer import IndexResult
    from ultrakb.llm_execution import active_executor, check_model_stop

    current = read_source(kb_dir, admission.source.source_id)
    if current.removed or current.target_revision_id != normalized.source_revision_id:
        raise ValueError("Index checkpoint source is no longer current")
    identities = _identities(normalized, index_input, active_executor())
    directory = checkpoint_root(kb_dir, current.source_id) / revision.unit_revision_id
    contained_paths(kb_dir, [directory])
    marker, data = directory / "checkpoint.json", directory / "data"
    if marker.exists():
        saved = IndexCheckpoint.model_validate_json(marker.read_text("utf-8"))
        if (
            saved.identity not in identities
            or saved.source_id != current.source_id
            or saved.source_revision_id != normalized.source_revision_id
            or saved.unit_revision_id != revision.unit_revision_id
        ):
            raise ValueError("Index checkpoint input or processing identity changed")
        files = wiki_versions(kb_dir, data)
        # SQLite readers can leave an empty WAL and a shared-memory cache beside
        # a sealed database. They contain no published index content. Dirty WALs
        # remain invalid, and every file in the saved manifest is still checked.
        wal = data / "index/context.sqlite-wal"
        if wal.is_file() and wal.stat().st_size:
            raise ValueError("Index checkpoint has uncheckpointed database writes")
        for name in ("index/context.sqlite-wal", "index/context.sqlite-shm"):
            if name not in saved.files:
                files.pop(name, None)
        if files != saved.files:
            raise ValueError("Index checkpoint is damaged or incomplete")
        if _navigation(_validate_index(data / "index", saved.document_id)) != _navigation(
            saved.tree["structure"]
        ):
            raise ValueError("Index checkpoint tree differs from its validated document")
        copy_index_package(data / "index", working / "index", document_id=saved.document_id)
        copy_tree(data / "wiki", working / "wiki")
        check_model_stop()
        return IndexResult(saved.document_id, saved.description, saved.tree)

    # An interrupted save without a marker is never a successful checkpoint.
    # Include it in the new transaction so a failed rebuild remains recoverable.
    indexed = build()
    if _navigation(_validate_index(working / "index", indexed.doc_id)) != _navigation(
        indexed.tree["structure"]
    ):
        raise ValueError("Index result differs from its validated document tree")
    check_model_stop()
    from ultrakb.ingest_records import ImportUnit
    from ultrakb.source_catalog import read_record

    unit = read_record(kb_dir, "units", revision.unit_id, ImportUnit)
    with mutation_scope(kb_dir, [directory], operation="retain-complete-index", checkpoint=True):
        import shutil

        if directory.exists():
            shutil.rmtree(directory)
        copy_index_package(
            working / "index",
            data / "index",
            owner_revision=revision.unit_revision_id,
            document_id=indexed.doc_id,
        )
        names = [
            f"summaries/{unit.doc_name}.md",
            *(f"sources/{unit.doc_name}{suffix}" for suffix in (".md", ".json", ".content.json")),
        ]
        for name in names:
            source = working / "wiki" / name
            if source.is_file():
                _copy_file_atomic(source, data / "wiki" / name)
        images = Path("sources/images") / unit.doc_name
        if (working / "wiki" / images).exists():
            copy_tree(working / "wiki" / images, data / "wiki" / images)
        saved = IndexCheckpoint(
            source_id=current.source_id,
            source_revision_id=normalized.source_revision_id,
            unit_revision_id=revision.unit_revision_id,
            identity=identities[0],
            document_id=indexed.doc_id,
            description=indexed.description,
            tree=indexed.tree,
            files=wiki_versions(kb_dir, data),
        )
        atomic_write_json(marker, saved.model_dump(mode="json"))
        check_model_stop()
    return indexed
