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
from .execution_store import _label, require_active_execution
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

    def for_work(
        self, model_budget_id: str, *, object_id: dict, conditions: dict, execution_id: str
    ):
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
            require_active_execution(connection, self.identity, execution_id)
            if connection.execute(
                "SELECT model_budget_id FROM ukb_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone() != (model_budget_id,):
                raise ValueError("Compile work belongs to another model budget")
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
        return CompileWork(
            self.root, self.identity, row[0], model_budget_id, object_digest, execution_id
        )


class CompileWork:
    request_version = 3
    structured_summary = True
    legacy_summary_format = False

    def __init__(self, root, identity, work_id, model_budget_id, object_digest, execution_id):
        self.root, self.identity, self.work_id = root, identity, work_id
        self.model_budget_id, self.object_digest = model_budget_id, object_digest
        self.execution_id = execution_id

    def _active(self, connection):
        require_active_execution(connection, self.identity, self.execution_id)
        self._provenance(connection)

    def _provenance(self, connection):
        if connection.execute(
            "SELECT model_budget_id, generation FROM ukb_executions WHERE execution_id = ?",
            (self.execution_id,),
        ).fetchone() != (self.model_budget_id, self.identity.generation):
            raise ValueError("Compile callback belongs to another execution generation")

    def _record(self, db, key, signature):
        _label(key, limit=4096)
        TypeAdapter(Digest).validate_python(signature)
        row = db.connection.execute(
            "SELECT request_digest, record_json FROM ukb_compile_records "
            "WHERE work_id = ? AND step_key = ?",
            (self.work_id, key),
        ).fetchone()
        if row and row[0] != signature:
            raise PageResponseError("compile_request_changed", "The frozen compile request changed")
        return json.loads(row[1]) if row else {}

    def _write_record(self, connection, key, signature, record):
        connection.execute(
            "INSERT INTO ukb_compile_records VALUES (?, ?, ?, ?) "
            "ON CONFLICT(work_id, step_key) DO UPDATE SET record_json = excluded.record_json",
            (self.work_id, key, signature, _json(record, bounded=True)),
        )

    def prepare_summary_delivery(self):
        from ultrakb.llm_execution import check_model_stop

        check_model_stop()
        return False  # New work always uses the current versioned compiler protocol.

    def save(
        self,
        key: str,
        request_digest: str,
        result,
        *,
        metadata: dict | None = None,
        reused_response: dict | None = None,
    ):
        _label(key, limit=4096)
        TypeAdapter(Digest).validate_python(request_digest)
        payload = _json(result).encode()
        from ultrakb.agent.compile_contract import ACCEPTANCE_VERSION

        metadata_payload = _json(
            {
                "acceptance_version": ACCEPTANCE_VERSION,
                "metadata": metadata or {},
                "reused_response": reused_response,
            },
        ).encode()
        with storage_stage(self.root, self.identity) as db:
            from ultrakb.llm_execution import check_model_stop

            check_model_stop()
            blob = put_blob(db, payload)
            attributes = put_blob(db, metadata_payload)
            encoded_metadata = _json({"metadata_digest": attributes.digest}, bounded=True)
            with db.transaction() as connection:
                self._active(connection)
                check_model_stop()
                self._record(db, key, request_digest)
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
                retain_blob(connection, "compile-step", self.work_id, "metadata:" + key, attributes)

    def load(self, key: str, request_digest: str):
        from ultrakb.llm_execution import check_model_stop

        check_model_stop()
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT result_digest FROM ukb_compile_steps WHERE work_id = ? "
                "AND step_key = ? AND request_digest = ?",
                (self.work_id, key, request_digest),
            ).fetchone()
            return json.loads(read_bytes(db, row[0])) if row else None

    def accepted_plan(self):
        from ultrakb.llm_execution import check_model_stop

        check_model_stop()
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT request_digest, result_digest FROM ukb_compile_steps "
                "WHERE work_id = ? AND step_key = 'plan'",
                (self.work_id,),
            ).fetchone()
            return (row[0], json.loads(read_bytes(db, row[1]))) if row else None

    def require_request(self, key: str, request_digest: str):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._record(db, key, request_digest)
            row = db.connection.execute(
                "SELECT request_digest FROM ukb_compile_steps WHERE work_id = ? AND step_key = ?",
                (self.work_id, key),
            ).fetchone()
            if row and row[0] != request_digest:
                raise PageResponseError(
                    "compile_request_changed", "The exact accepted compile input no longer matches"
                )

    def bind_page(self, page: str, base_sha256: str, *, page_path: Path | None = None):
        _path(page)
        _label(page, limit=4096)
        TypeAdapter(Digest).validate_python(base_sha256)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            self._active(connection)
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

    def failure_code(self, key, request_digest):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._record(db, key, request_digest).get("failure_code")

    def record_failure(self, key, request_digest, code):
        _label(code)
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            self._provenance(connection)
            record = self._record(db, key, request_digest)
            self._write_record(connection, key, request_digest, {**record, "failure_code": code})

    def require_generation(self, key, request_digest):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if self._record(db, key, request_digest).get("repair_used"):
                raise PageResponseError(
                    "compile_repair_exhausted", "The retained response repair was already consumed"
                )

    def _invocation(self, connection, key, signature, stage, kind):
        _label(stage, limit=1024)
        identity = uuid4().hex
        connection.execute(
            "INSERT INTO ukb_compile_invocations(invocation_id, work_id, step_key, request_digest, "
            "execution_id, stage, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                identity,
                self.work_id,
                key,
                signature,
                self.execution_id,
                stage.removeprefix("compile."),
                kind,
            ),
        )
        return identity

    def claim_generation(self, key, request_digest, *, stage):
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            self._active(connection)
            record = self._record(db, key, request_digest)
            if record.get("repair_used"):
                raise PageResponseError(
                    "compile_repair_exhausted", "The retained response repair was already consumed"
                )
            invocation = self._invocation(connection, key, request_digest, stage, "generation")
            self._write_record(
                connection, key, request_digest, {**record, "latest_invocation": invocation}
            )

    def claim_repair(self, key, request_digest):
        self._claim_once(key, request_digest, "repair")

    def claim_conflict(self, key, request_digest):
        self._claim_once(key, request_digest, "conflict")

    def _claim_once(self, key, signature, kind):
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            self._active(connection)
            record = self._record(db, key, signature)
            flag = kind + "_used"
            if record.get(flag):
                raise PageResponseError(
                    "compile_" + kind + "_exhausted", "The retained allowance was already consumed"
                )
            previous = connection.execute(
                "SELECT stage FROM ukb_compile_invocations WHERE work_id = ? AND step_key = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (self.work_id, key),
            ).fetchone()
            invocation = self._invocation(
                connection, key, signature, previous[0] if previous else key, kind
            )
            self._write_record(
                connection, key, signature, {**record, flag: True, "latest_invocation": invocation}
            )

    def model_work_key(self, stage):
        stage = stage.removeprefix("compile.").removesuffix(" (repair)")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT invocation_id FROM ukb_compile_invocations WHERE work_id = ? "
                "AND execution_id = ? AND stage = ? ORDER BY sequence DESC LIMIT 1",
                (self.work_id, self.execution_id, stage),
            ).fetchone()
            return "compile:" + row[0] if row else None

    def retained_response(self, key, request_digest, *, protocol, sections, stage):
        from .compile_deliveries import retained_response

        return retained_response(self, key, request_digest, protocol, sections, stage)

    def record_invalid_response(self, key, request_digest, response, **metadata):
        from .compile_deliveries import capture_response

        return capture_response(self, key, request_digest, response, metadata)

    def record_received_response(self, key, request_digest, response, **metadata):
        return self.record_invalid_response(
            key,
            request_digest,
            response,
            received=True,
            detail="Received; awaiting source validation",
            **metadata,
        )

    def record_delivery_resolution(self, key, request_digest, raw, delivered, resolutions):
        from .compile_deliveries import capture_resolution

        return capture_resolution(self, key, request_digest, raw, delivered, resolutions)
