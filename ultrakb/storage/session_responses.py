"""Replay exact conversation-analysis receipts independently of tree acceptance."""

import json

from ultrakb.compile_checkpoints import digest
from ultrakb.execution_capacity import request_input
from ultrakb.mutation import RecoveryRequired

from .blobs import read_bytes
from .coordination import storage_stage
from .session_store import SessionStore


class ConversationResponses:
    def __init__(self, store: SessionStore, session_id: str, policy_key: str):
        self.store, self.session_id, self.policy_key = store, session_id, policy_key

    def complete(self, executor, request) -> str:
        executor.policy.check()
        logical_id = digest(
            {
                "protocol": "conversation-analysis-response-v1",
                "kb_id": self.store.identity.kb_id,
                "session_id": self.session_id,
                "policy_key": self.policy_key,
                "stage": request.stage,
                "prompt_version": request.prompt_version,
                "input": request_input(executor.prepare_request("conversation_index", request)),
            }
        )[:32]  # Match the ledger's 128-bit logical-call identity format.
        received = self._received(logical_id)
        if received is not None:
            executor.policy.check()
            return received
        # The gateway retains this identity before sending and owns response durability.
        # Release storage protection while waiting on the model.
        return executor.complete("conversation_index", request, logical_id=logical_id).text

    def _received(self, logical_id):
        with storage_stage(self.store.root, self.store.identity, read_only=True) as db:
            SessionStore._current(db, self.session_id)
            # Ignore unsent attempts without results, preserving earlier paid attempts.
            # SDK cache hits also have durable responses, despite having no network send.
            # The logical-call index bounds this lookup to the exact frozen request.
            row = db.connection.execute(
                "SELECT r.payload_json, p.response_digest FROM ukb_usage_requests r "
                "LEFT JOIN ukb_response_receipts p ON p.request_id = r.request_id "
                "WHERE r.logical_call_id = ? "
                "AND (p.response_digest IS NOT NULL OR "
                "coalesce(json_extract(r.payload_json, '$.send_status'), 'unknown') "
                "!= 'not_sent') ORDER BY r.sequence DESC LIMIT 1",
                (logical_id,),
            ).fetchone()
            if row is None:
                return None
            if row[1] is None:
                raise RecoveryRequired("The conversation analysis needs response receipt recovery")
            try:
                value = json.loads(read_bytes(db, row[1]))
                original = json.loads(row[0])
                if (
                    original.get("state") == "failed"
                    and isinstance(value, dict)
                    and isinstance(value.get("error"), dict)
                    and "choices" not in value
                ):
                    return None  # A known provider error is not a completed computation.
                [choice] = value["choices"]
                text = choice["message"]["content"]
                if not isinstance(text, str) or choice["finish_reason"] not in {"stop", "length"}:
                    raise ValueError("Expected a complete conversation-analysis response")
                return text
            except (ValueError, TypeError, KeyError) as error:
                raise RecoveryRequired("The conversation analysis response needs repair") from error
