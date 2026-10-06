"""Complete PageIndex StorageEngine implemented on one native ConDB transaction."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from typing import Any

from contextdb import ConDB, TreeDB
from contextdb.core.json_values import validate_json
from pageindex.errors import CollectionAlreadyExistsError, CollectionNotFoundError

from openkb.index_location import IndexLocation

_SCHEMA_VERSION = 1
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _name(value: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("Invalid collection or document identity")


class ConDBPageIndexStorage:
    def __init__(self, location: IndexLocation):
        self.location = location
        self._closed = False
        if not location.read_only:
            location.database.parent.mkdir(parents=True, exist_ok=True)
        self.db = TreeDB(str(location.database), read_only=location.read_only)
        try:
            self._schema()
        except BaseException:
            self.db.close()
            raise
        self.context = ConDB(storage=self.db)

    def _schema(self) -> None:
        tables = {
            r[0] for r in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "okb_index_schema" in tables:
            versions = [r[0] for r in self.db.conn.execute("SELECT version FROM okb_index_schema")]
            if versions != [_SCHEMA_VERSION]:
                raise ValueError("Unsupported OpenKB index schema")
            for table, required in {
                "okb_collections": {"name"},
                "okb_documents": {
                    "doc_id",
                    "collection_name",
                    "tree_id",
                    "file_hash",
                    "processing_key",
                    "payload",
                },
            }.items():
                columns = {r[1] for r in self.db.conn.execute(f"PRAGMA table_info({table})")}
                if columns != required:
                    raise ValueError(f"Invalid OpenKB index schema: {table}")
            return
        if self.location.read_only or any(name.startswith("okb_") for name in tables):
            raise ValueError("Missing or unsupported OpenKB index schema")
        with self.db.transaction() as db:
            db.execute("CREATE TABLE okb_index_schema (version INTEGER NOT NULL)")
            db.execute("INSERT INTO okb_index_schema VALUES (?)", (_SCHEMA_VERSION,))
            db.execute("CREATE TABLE okb_collections (name TEXT PRIMARY KEY)")
            db.execute("""CREATE TABLE okb_documents (
                doc_id TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL REFERENCES okb_collections(name),
                tree_id TEXT NOT NULL UNIQUE REFERENCES trees(tree_id),
                file_hash TEXT NOT NULL,
                processing_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(collection_name, processing_key)
            )""")

    def create_collection(self, name: str) -> None:
        _name(name)
        with self.db.transaction() as db:
            try:
                db.execute("INSERT INTO okb_collections VALUES (?)", (name,))
            except sqlite3.IntegrityError as exc:
                raise CollectionAlreadyExistsError(f"Collection '{name}' already exists") from exc

    def get_or_create_collection(self, name: str) -> None:
        _name(name)
        if name in self.list_collections():
            return
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO okb_collections VALUES (?)", (name,))

    def list_collections(self) -> list[str]:
        return [
            row[0] for row in self.db.conn.execute("SELECT name FROM okb_collections ORDER BY name")
        ]

    def delete_collection(self, name: str) -> None:
        with self.db.transaction() as db:
            for doc in self.list_documents(name):
                self.delete_document(name, doc["doc_id"])
            db.execute("DELETE FROM okb_collections WHERE name = ?", (name,))

    def save_document(self, collection: str, doc_id: str, doc: dict) -> None:
        _name(collection)
        _name(doc_id)
        validate_json(doc)
        payload = copy.deepcopy(doc)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("Complete document metadata is required")
        source_hash = metadata.get("source_digest")
        processing_key = payload.get(
            "processing_key", metadata.get("processing_fingerprint", payload.get("file_hash"))
        )
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("Document requires its original source digest")
        if not isinstance(processing_key, str) or not processing_key:
            raise ValueError("Document requires its processing key")
        if not isinstance(payload.get("file_path"), str):
            raise ValueError("Document requires a managed source input")
        payload["file_path"] = self.location.input_reference(payload["file_path"])
        payload["doc_id"] = doc_id
        from openkb.index_documents import validate_document

        validate_document(payload, self.location, collection, doc_id)
        with self.db.transaction() as db:
            if collection not in self.list_collections():
                raise CollectionNotFoundError(f"Collection '{collection}' does not exist")
            tree_id = self.context.store(payload, format="document")
            db.execute(
                """INSERT INTO okb_documents
                (doc_id, collection_name, tree_id, file_hash, processing_key, payload)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    doc_id,
                    collection,
                    tree_id,
                    source_hash,
                    processing_key,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def find_document_by_hash(self, collection: str, file_hash: str) -> str | None:
        # PageIndex's historic method name accepts its processing/cache key,
        # which includes source bytes AND content policy. Raw SHA is separate.
        row = self.db.conn.execute(
            "SELECT doc_id FROM okb_documents WHERE collection_name = ? AND processing_key = ?",
            (collection, file_hash),
        ).fetchone()
        return row[0] if row else None

    def _document(self, collection: str, doc_id: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT payload FROM okb_documents WHERE collection_name = ? AND doc_id = ?",
            (collection, doc_id),
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def get_document(self, collection: str, doc_id: str) -> dict:
        value = self._document(collection, doc_id)
        if value:
            value.pop("pages", None)
            value["file_path"] = str(self.location.input_path(value["file_path"]))
        return value

    def get_document_structure(self, collection: str, doc_id: str) -> list:
        return self._document(collection, doc_id).get("structure", [])

    def get_pages(self, collection: str, doc_id: str) -> list | None:
        return self._document(collection, doc_id).get("pages")

    def list_documents(self, collection: str) -> list[dict]:
        return [
            {
                key: value.get(key, "")
                for key in ("doc_id", "doc_name", "doc_description", "doc_type")
            }
            for row in self.db.conn.execute(
                "SELECT payload FROM okb_documents WHERE collection_name = ? ORDER BY rowid",
                (collection,),
            )
            for value in [json.loads(row[0])]
        ]

    def delete_document(self, collection: str, doc_id: str) -> None:
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT tree_id FROM okb_documents WHERE collection_name = ? AND doc_id = ?",
                (collection, doc_id),
            ).fetchone()
            if row:
                db.execute(
                    "DELETE FROM okb_documents WHERE collection_name = ? AND doc_id = ?",
                    (collection, doc_id),
                )
                self.db.delete_tree(row[0])

    def close(self) -> None:
        if not self._closed:
            try:
                self.db.checkpoint()
            finally:
                self.db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
