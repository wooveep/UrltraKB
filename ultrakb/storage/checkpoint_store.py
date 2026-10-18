"""Independently accepted compile work, bound to exact input and model policy."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from contextdb.core.json_values import validate_json
from pydantic import TypeAdapter

from ultrakb.agent.page_response import PageResponseError
from ultrakb.source_records import Digest, RecordId

from .blobs import put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .database import DatabaseIdentity
from .execution_store import _label
from .knowledge_store import _path


def _json(value, *, bounded=False):
    validate_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if bounded and len(encoded.encode()) > 64 * 1024:
        raise ValueError("Compile metadata exceeds the storage item limit")
    return encoded


class CompileCheckpointStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def for_work(self, model_budget_id: str, *, object_id: dict, conditions: dict):
        TypeAdapter(RecordId).validate_python(model_budget_id)
        if not isinstance(object_id, dict) or not isinstance(conditions, dict):
            raise ValueError("Compile work requires explicit object and policy records")
        object_json, conditions_json = (
            _json(object_id, bounded=True),
            _json(conditions, bounded=True),
        )
        # Their combined metadata is one write unit, so bound it together as well.
        if len(object_json.encode()) + len(conditions_json.encode()) > 64 * 1024:
            raise ValueError("Compile work metadata exceeds the storage item limit")
        object_digest = hashlib.sha256(object_json.encode()).hexdigest()
        conditions_digest = hashlib.sha256(conditions_json.encode()).hexdigest()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_compile_work VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(model_budget_id, object_digest, conditions_digest) DO NOTHING",
                (
                    uuid4().hex,
                    model_budget_id,
                    object_digest,
                    conditions_digest,
                    object_json,
                    conditions_json,
                ),
            )
            row = connection.execute(
                "SELECT work_id, object_json, conditions_json FROM ukb_compile_work "
                "WHERE model_budget_id = ? AND object_digest = ? AND conditions_digest = ?",
                (model_budget_id, object_digest, conditions_digest),
            ).fetchone()
            if row[1:] != (object_json, conditions_json):
                raise ValueError("Compile work metadata failed its content identity")
        return CompileWork(self.root, self.identity, row[0], model_budget_id, object_digest)


class CompileWork:
    def __init__(self, root, identity, work_id, model_budget_id, object_digest):
        self.root, self.identity, self.work_id = root, identity, work_id
        self.model_budget_id, self.object_digest = model_budget_id, object_digest

    def save(self, key: str, request_digest: str, result, *, metadata: dict | None = None):
        _label(key, limit=4096)
        TypeAdapter(Digest).validate_python(request_digest)
        payload = _json(result).encode()
        encoded_metadata = _json(metadata or {}, bounded=True)
        if len(encoded_metadata.encode()) + len(key.encode()) > 64 * 1024:
            raise ValueError("Compile step metadata exceeds the storage item limit")
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, payload)
            with db.transaction() as connection:
                previous = connection.execute(
                    "SELECT request_digest, result_digest FROM ukb_compile_steps "
                    "WHERE work_id = ? AND step_key = ?",
                    (self.work_id, key),
                ).fetchone()
                if previous:
                    if previous != (request_digest, blob.digest):
                        raise PageResponseError(
                            "compile_request_changed", "An accepted compile step is immutable"
                        )
                    return
                connection.execute(
                    "INSERT INTO ukb_compile_steps VALUES (?, ?, ?, ?, ?)",
                    (self.work_id, key, request_digest, blob.digest, encoded_metadata),
                )
                retain_blob(connection, "compile-step", self.work_id, key, blob)

    def load(self, key: str, request_digest: str):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT result_digest FROM ukb_compile_steps WHERE work_id = ? "
                "AND step_key = ? AND request_digest = ?",
                (self.work_id, key, request_digest),
            ).fetchone()
            return json.loads(read_bytes(db, row[0])) if row else None

    def require_request(self, key: str, request_digest: str):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT request_digest FROM ukb_compile_steps WHERE work_id = ? AND step_key = ?",
                (self.work_id, key),
            ).fetchone()
            if row and row[0] != request_digest:
                raise PageResponseError(
                    "compile_request_changed", "The exact accepted compile input no longer matches"
                )

    def accepted_plan(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT request_digest, result_digest FROM ukb_compile_steps "
                "WHERE work_id = ? AND step_key = 'plan'",
                (self.work_id,),
            ).fetchone()
            return (row[0], json.loads(read_bytes(db, row[1]))) if row else None

    def bind_page(self, page: str, base_sha256: str):
        _path(page)
        _label(page, limit=4096)
        TypeAdapter(Digest).validate_python(base_sha256)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            previous = connection.execute(
                "SELECT base_digest FROM ukb_compile_page_bases WHERE model_budget_id = ? "
                "AND object_digest = ? AND page_path = ?",
                (self.model_budget_id, self.object_digest, page),
            ).fetchone()
            if previous and previous[0] != base_sha256:
                raise PageResponseError(
                    "page_update_stale", "The accepted page update base changed"
                )
            connection.execute(
                "INSERT INTO ukb_compile_page_bases VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (self.model_budget_id, self.object_digest, page, base_sha256),
            )
