"""Resume a retained answer or gate resending an unresolved conversation attempt."""

import json

from ultrakb.mutation import RecoveryRequired

from .blobs import read_bytes
from .coordination import storage_stage


def submission_execution(store, session_id, submission_id, *, view_id, cancelled=None):
    """Find the original budget through the exact native submission identity."""
    with storage_stage(store.root, store.identity, read_only=True, cancelled=cancelled) as db:
        row = db.connection.execute(
            "SELECT r.execution_id FROM ukb_conversation_runs r "
            "JOIN ukb_executions e ON e.execution_id = r.execution_id "
            "JOIN ukb_conversations c ON c.session_id = r.session_id "
            "WHERE r.session_id = ? AND r.submission_id = ? AND c.view_id = ? "
            "AND c.deleted = 0 "
            "AND NOT EXISTS (SELECT 1 FROM ukb_turns t WHERE t.session_id = r.session_id "
            "AND t.attempt_id = r.submission_id) "
            "AND NOT EXISTS (SELECT 1 FROM ukb_conversation_answers a "
            "JOIN ukb_conversation_runs accepted ON accepted.execution_id = a.execution_id "
            "WHERE accepted.session_id = r.session_id "
            "AND accepted.submission_id = r.submission_id) "
            "ORDER BY e.sequence DESC LIMIT 1",
            (session_id, submission_id, view_id),
        ).fetchone()
    return row[0] if row else None


def recover_submission(store, session_id, submission_id, message, *, execution_id, check_cancelled):
    """Accept a retained complete answer; never resend an unconfirmed paid attempt."""
    with storage_stage(store.root, store.identity, read_only=True) as db:
        store._require_active_execution(db.connection, execution_id)
        candidate = db.connection.execute(
            "SELECT a.execution_id, a.turn_id, a.input_revision, a.payload_digest, "
            "a.result_digest FROM ukb_conversation_answers a JOIN ukb_conversation_runs r "
            "ON r.execution_id = a.execution_id JOIN ukb_executions e "
            "ON e.execution_id = r.execution_id WHERE r.session_id = ? AND r.submission_id = ? "
            "AND r.execution_id != ? ORDER BY e.sequence DESC LIMIT 1",
            (session_id, submission_id, execution_id),
        ).fetchone()
        if candidate is None:
            pending = db.connection.execute(
                "SELECT 1 FROM ukb_conversation_runs r JOIN ukb_usage_counters c "
                "ON c.bucket = 'execution' AND c.identity = r.execution_id "
                "AND c.stage_code = '' "
                "WHERE r.session_id = ? AND r.submission_id = ? AND r.execution_id != ? "
                "AND (c.send_unknown > 0 OR c.in_flight_requests > 0 "
                "OR (c.actual_sends > 0 AND c.collection_pending > 0) "
                "OR EXISTS (SELECT 1 FROM ukb_usage_requests q "
                "WHERE q.execution_id = r.execution_id "
                "AND json_extract(q.payload_json, '$.send_status') != 'not_sent' "
                "AND (json_extract(q.payload_json, '$.state') != 'failed' "
                "OR json_extract(q.payload_json, '$.observation') != 'transport'))) "
                "LIMIT 1",
                (session_id, submission_id, execution_id),
            ).fetchone()
            if pending:
                raise RecoveryRequired("The earlier paid conversation attempt needs recovery")
            return False
        turn = json.loads(read_bytes(db, candidate[3]))
        metadata = json.loads(read_bytes(db, candidate[4]))
        messages = {m["role"]: m["content"] for m in turn["messages"]}
        if messages["user"] != message:
            raise ValueError("Conversation submission identity mismatch")
    store.append_exchange(
        session_id,
        candidate[1],
        message,
        messages["assistant"],
        system=messages.get("system"),
        attempt_id=submission_id,
        expected_revision=candidate[2],
        check_cancelled=check_cancelled,
        metadata=metadata,
        execution_id=execution_id,
        reused_from=candidate[0],
    )
    return True
