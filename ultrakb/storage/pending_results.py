"""Retain a source import's delivered per-unit facts before marking its queue attempt done."""

import json

from pydantic import TypeAdapter

from ultrakb.ingest_result import IngestResult
from ultrakb.llm_usage_models import validate_receipt

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogConflict
from .coordination import storage_stage
from .execution_store import execution_state, now
from .pending_store import require_running_work

_RESULT = TypeAdapter(IngestResult)


class PendingResults:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def read(self, attempt_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT payload_digest, status FROM ukb_pending_import_results "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return (_RESULT.validate_json(read_bytes(db, row[0])), row[1]) if row else None

    def save(self, work, execution, result):
        validate_receipt(result.model_usage)
        for unit in result.units:
            validate_receipt(unit.model_usage)
        encoded = _RESULT.dump_json(result)
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Pending import result exceeds the storage payload limit")
        status = (
            "completed"
            if result.status in {"added", "skipped"}
            else "not_imported"
            if result.status == "rejected" and result.source_id is None
            else result.status
            if result.status in {"partial", "failed", "stopped"}
            else "blocked"
        )
        summary = json.dumps(
            {
                "source_id": result.source_id,
                "source_revision_id": result.source_revision_id,
                "publication_id": result.publication_id,
                "projection_pending": result.projection_pending,
                "quality_known": result.status in {"added", "skipped", "partial"}
                or bool(result.quality),
                "quality_count": len(result.quality),
                "unfinished_count": len(result.unfinished),
            }
        )
        with storage_stage(self.root, self.identity) as db:
            if result.source_id is not None:
                source = db.connection.execute(
                    "SELECT r.source_id, b.source_revision_id FROM ukb_pending_source_bindings b "
                    "JOIN ukb_source_revisions r ON r.revision_id = b.source_revision_id "
                    "WHERE b.intent_id = ?",
                    (work.intent_id,),
                ).fetchone()
                if source != (result.source_id, result.source_revision_id):
                    raise ValueError("Pending import result belongs to another admitted source")
            if status == "completed":
                if not result.units or not all(
                    unit.status in {"completed", "empty", "retired"} for unit in result.units
                ):
                    raise ValueError(
                        "Pending import completion requires all delivered unit outcomes"
                    )
                if (
                    db.connection.execute(
                        "SELECT 1 FROM ukb_publication_receipts WHERE publication_id = ?",
                        (result.publication_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError(
                        "Pending import completion requires its exact saved publication"
                    )
            blob, timestamp = put_blob(db, encoded), now()
            with db.transaction() as connection:
                require_running_work(
                    connection, self.identity, work, allow_cancelled=True, check_input=False
                )
                if execution_state(connection, self.identity, execution.execution_id) not in {
                    "started",
                    "stopping",
                }:
                    raise CatalogConflict("A terminated execution cannot deliver an import result")
                previous = connection.execute(
                    "SELECT payload_digest, status FROM ukb_pending_import_results "
                    "WHERE attempt_id = ?",
                    (work.attempt_id,),
                ).fetchone()
                if previous is not None and previous != (blob.digest, status):
                    raise CatalogConflict("The import attempt already has another delivered result")
                connection.execute(
                    "INSERT INTO ukb_pending_import_results VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (work.attempt_id, execution.execution_id, blob.digest, status, summary),
                )
                retain_blob(connection, "pending-result", work.attempt_id, "result", blob)
                connection.execute(
                    "UPDATE ukb_pending_attempts SET status = ?, publication_id = ?, "
                    "finished_at = ? "
                    "WHERE attempt_id = ? AND status = 'started'",
                    (status, result.publication_id, timestamp, work.attempt_id),
                )
                connection.execute(
                    "UPDATE ukb_import_intents SET status = ? WHERE intent_id = ?",
                    (status, work.intent_id),
                )
        return status
