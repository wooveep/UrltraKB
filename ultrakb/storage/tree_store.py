"""Bounded ConDB builds, seal validation, and fixed tree read stages."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from contextdb import ConDB, TreeDB
from pydantic import TypeAdapter

from ultrakb.source_records import Digest

from .blobs import put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .database import DatabaseIdentity


@dataclass(frozen=True)
class TreeRevision:
    tree_id: str
    protocol: str
    input_fingerprint: str
    manifest_digest: str


class BoundTree:
    """A fixed tree capability; callers cannot redirect it to another revision."""

    def __init__(self, client, revision, root_id):
        self._client, self.revision, self.root_id = client, revision, root_id

    def content(self, node_id):
        return self._client.get_content(self.revision.tree_id, node_id)

    def children(self, node_id):
        return self._client.get_children(self.revision.tree_id, node_id)

    def query(self, question, **options):
        return self._client.query(self.revision.tree_id, question, **options)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _content_slot(kind, row):
    return f"{'entity' if kind == 'entities' else 'node'}:{row[1]}"


def _batches(rows):
    batch, byte_count = [], 0
    for kind, row, blob in rows:
        encoded_size = len(_json(row))
        if blob:
            encoded_size += len(
                _json(("tree-build", row[0], _content_slot(kind, row), blob.digest))
            )
        if encoded_size > 64 * 1024:
            raise ValueError("A tree row exceeds the encoded item limit")
        if batch and (len(batch) >= 63 or byte_count + encoded_size > 256 * 1024):
            yield batch
            batch, byte_count = [], 0
        batch.append((kind, row, blob))
        byte_count += encoded_size
    if batch:
        yield batch


def retain_tree(connection, owner_kind: str, owner_id: str, slot: str, tree_id: str):
    if not connection.in_transaction:
        raise RuntimeError("A tree reference must commit with its business owner")
    inserted = connection.execute(
        "INSERT INTO ukb_tree_refs VALUES (?, ?, ?, ?) "
        "ON CONFLICT(owner_kind, owner_id, slot) DO NOTHING",
        (owner_kind, owner_id, slot, tree_id),
    ).rowcount
    if not inserted and connection.execute(
        "SELECT tree_id FROM ukb_tree_refs WHERE owner_kind = ? AND owner_id = ? AND slot = ?",
        (owner_kind, owner_id, slot),
    ).fetchone() != (tree_id,):
        raise ValueError("An immutable tree binding cannot be reassigned")


class TreeStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def build(
        self, data: dict, *, protocol: str, input_fingerprint: str, meta: dict | None = None
    ) -> TreeRevision:
        if protocol not in {"document", "chatindex", "generic"}:
            raise ValueError("Unknown tree content protocol")
        TypeAdapter(Digest).validate_python(input_fingerprint)
        prepared = ConDB.prepare(data, format=protocol, meta=meta)
        tree_id = prepared.tree_id
        if prepared.meta_json and len(prepared.meta_json.encode()) > 64 * 1024:
            raise ValueError("Tree metadata exceeds the encoded item limit")
        with storage_stage(self.root, self.identity) as db:
            entities, nodes, content_refs = [], [], {}
            for row in sorted(prepared.entities, key=lambda row: row[1]):
                if len(row[3].encode()) > 16 * 1024:
                    blob = put_blob(db, row[3].encode())
                    content_refs[_content_slot("entities", row)] = blob
                    row = (*row[:3], json.dumps(f"condb.sha256:{blob.digest}"), row[4])
                entities.append(row)
            for row in sorted(prepared.nodes, key=lambda row: row[6]):
                if row[9] and len(row[9].encode()) > 16 * 1024:
                    blob = put_blob(db, row[9].encode())
                    content_refs[_content_slot("nodes", row)] = blob
                    row = (*row[:9], json.dumps(f"condb.sha256:{blob.digest}"), *row[10:])
                nodes.append(row)
            tree_row = (
                tree_id,
                prepared.root_id,
                prepared.timestamp,
                prepared.timestamp,
                prepared.meta_json,
            )
            manifest = put_blob(
                db,
                _json(
                    {
                        "tree": tree_row,
                        "nodes": nodes,
                        "entities": entities,
                        "protocol": protocol,
                        "input_fingerprint": input_fingerprint,
                    }
                ),
            )
            generation = uuid4().hex
            with db.transaction() as connection:
                connection.execute("INSERT INTO trees VALUES (?, ?, ?, ?, ?)", tree_row)
                connection.execute(
                    "INSERT INTO ukb_tree_builds VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
                    (
                        tree_id,
                        protocol,
                        input_fingerprint,
                        generation,
                        manifest.digest,
                        len(nodes),
                        len(entities),
                    ),
                )
                retain_blob(connection, "tree-build", tree_id, "manifest", manifest)
            rows = [
                (kind, row, content_refs.get(_content_slot(kind, row)))
                for kind, records in (("entities", entities), ("nodes", nodes))
                for row in records
            ]
            # Prepare/encode each bounded batch before acquiring the single writer.
            for batch in _batches(rows):
                with db.transaction() as connection:
                    for kind, row, blob in batch:
                        placeholders = ",".join("?" for _ in row)
                        connection.execute(f"INSERT INTO {kind} VALUES ({placeholders})", row)
                        if blob:
                            retain_blob(
                                connection, "tree-build", tree_id, _content_slot(kind, row), blob
                            )
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE ukb_tree_builds SET state = 'sealed' WHERE tree_id = ? "
                    "AND state = 'draft' AND build_generation = ?",
                    (tree_id, generation),
                )
            self._validate_sealed(db, tree_id, generation)
            with db.transaction() as connection:
                changed = connection.execute(
                    "UPDATE ukb_tree_builds SET state = 'ready' WHERE tree_id = ? "
                    "AND state = 'sealed' AND build_generation = ? AND manifest_digest = ?",
                    (tree_id, generation, manifest.digest),
                ).rowcount
                if changed != 1:
                    raise ValueError("The tree changed after sealed validation")
            return TreeRevision(tree_id, protocol, input_fingerprint, manifest.digest)

    @staticmethod
    def _validate_sealed(db, tree_id, generation):
        record = db.connection.execute(
            "SELECT protocol, input_fingerprint, manifest_digest, node_count, entity_count "
            "FROM ukb_tree_builds WHERE tree_id = ? AND state = 'sealed' AND build_generation = ?",
            (tree_id, generation),
        ).fetchone()
        if not record:
            raise ValueError("The exact sealed tree is unavailable")
        tree = db.connection.execute("SELECT * FROM trees WHERE tree_id = ?", (tree_id,)).fetchone()
        nodes = db.connection.execute(
            "SELECT * FROM nodes WHERE tree_id = ? ORDER BY path", (tree_id,)
        ).fetchall()
        entities = db.connection.execute(
            "SELECT * FROM entities WHERE tree_id = ? ORDER BY entity_id", (tree_id,)
        ).fetchall()
        encoded = _json(
            {
                "tree": tree,
                "nodes": nodes,
                "entities": entities,
                "protocol": record[0],
                "input_fingerprint": record[1],
            }
        )
        if (len(nodes), len(entities)) != record[3:] or hashlib.sha256(
            encoded
        ).hexdigest() != record[2]:
            raise ValueError("Sealed tree rows do not match their exact prepared manifest")
        if read_bytes(db, record[2]) != encoded:
            raise ValueError("Sealed tree manifest failed integrity validation")
        references = dict(
            db.connection.execute(
                "SELECT slot, digest FROM ukb_blob_refs "
                "WHERE owner_kind = 'tree-build' AND owner_id = ?",
                (tree_id,),
            ).fetchall()
        )
        expected_refs = {"manifest": record[2]}
        for kind, records, column in (("entities", entities, 3), ("nodes", nodes, 9)):
            for row in records:
                payload = json.loads(row[column]) if row[column] else None
                if isinstance(payload, str) and payload.startswith("condb.sha256:"):
                    digest = payload.removeprefix("condb.sha256:")
                    TypeAdapter(Digest).validate_python(digest)
                    read_bytes(db, digest)
                    expected_refs[_content_slot(kind, row)] = digest
        if references != expected_refs:
            raise ValueError("Sealed tree content references are incomplete")
        storage = TreeDB(
            connection=db.connection,
            read_only=True,
            content_reader=lambda digest: read_bytes(db, digest).decode(),
        )
        root = storage.get_node(tree_id, tree[1])
        if root is None or root.parent_id is not None or root.depth != 0:
            raise ValueError("Sealed tree does not have its declared root")
        seen = set()
        for row in nodes:
            if row[2] is not None and row[2] not in seen:
                raise ValueError("Sealed tree is disconnected or has invalid traversal order")
            seen.add(row[1])
            if row[8] is not None:
                entity = storage.get_entity(tree_id, row[1])
                if entity is None:
                    raise ValueError("Sealed tree has an unresolved entity")

    @contextmanager
    def open(self, tree_id: str):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT protocol, input_fingerprint, manifest_digest FROM ukb_tree_builds "
                "WHERE tree_id = ? AND state = 'ready'",
                (tree_id,),
            ).fetchone()
            if not row:
                raise ValueError("Only complete ready trees can be read")
            storage = TreeDB(
                connection=db.connection,
                read_only=True,
                content_reader=lambda digest: read_bytes(db, digest).decode(),
            )
            try:
                yield BoundTree(
                    ConDB(storage=storage),
                    TreeRevision(tree_id, *row),
                    storage.get_root_id(tree_id),
                )
            finally:
                storage.close()
