"""One explicit extra summary delivery, retaining original repair and budget facts."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from .blobs import read_bytes
from .checkpoint_store import _json
from .coordination import storage_stage

_REQUESTED = ContextVar("failed_summary_recovery", default=False)


@contextmanager
def summary_recovery_requested(requested):
    if type(requested) is not bool:
        raise ValueError("Failed-summary recovery requires an explicit boolean")
    token = _REQUESTED.set(requested)
    try:
        yield
    finally:
        _REQUESTED.reset(token)


def authorize_summary_recovery(work):
    if not _REQUESTED.get():
        return
    with storage_stage(work.root, work.identity) as db:
        work._active(db.connection)
        work._require_current_target(db.connection)
        rows = db.connection.execute(
            "SELECT r.step_key, r.request_digest, r.record_json FROM ukb_compile_records r "
            "WHERE r.work_id = ? AND (r.step_key = 'short-summary' "
            "OR r.step_key LIKE 'summary:%') AND NOT EXISTS "
            "(SELECT 1 FROM ukb_compile_steps s WHERE s.work_id = r.work_id "
            "AND s.step_key = r.step_key)",
            (work.work_id,),
        ).fetchall()
        for key, signature, previous in rows:
            record = json.loads(previous)
            if (
                not record.get("repair_used")
                or not record.get("failure_code")
                or record.get("summary_recovery_granted")
            ):
                continue
            received = db.connection.execute(
                "SELECT metadata_digest FROM ukb_compile_responses WHERE response_id = ? "
                "AND work_id = ? AND step_key = ? AND request_digest = ?",
                (record.get("latest_response"), work.work_id, key, signature),
            ).fetchone()
            metadata = json.loads(read_bytes(db, received[0])) if received else {}
            if (
                metadata.get("invocation_id") != record.get("latest_invocation")
                or metadata.get("code") in {None, "response_received"}
                or metadata.get("response_protocol")
                not in {
                    "summary-json",
                    "summary-parts-json",
                    "summary-page-json",
                }
            ):
                continue
            encoded = _json(
                {**record, "summary_recovery_granted": True, "summary_recovery_authorized": True},
                bounded=True,
            )
            with db.transaction() as connection:
                work._active(connection)
                work._require_current_target(connection)
                work._write_record(connection, key, signature, previous, encoded)
