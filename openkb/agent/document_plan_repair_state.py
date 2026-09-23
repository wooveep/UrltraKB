"""Bounded, durable repair state for one rejected planning window."""

from __future__ import annotations

import json
from typing import Any

from openkb.agent.document_plan_feedback import RepairScopeError, authorize_repair, retry_messages
from openkb.agent.document_window_receipts import window_receipt_id
from openkb.agent.evidence_wire import WireMessages
from openkb.implementation import module_revision
from openkb.processing import ProcessingIncomplete
from openkb.sources import content_id


class PlanningRepairSession:
    def __init__(
        self,
        messages: WireMessages,
        decode_kwargs: dict[str, Any],
        checkpoints: Any,
        window: dict[str, Any],
        predecessor: str,
        limits: Any,
    ) -> None:
        self.messages = messages
        self.decode_kwargs = decode_kwargs
        self.checkpoints = checkpoints
        self.key = content_id(
            {
                "input": checkpoints.input,
                "window": window_receipt_id(window),
                "predecessor": predecessor,
                "request": [messages[0]["content"], messages[-1]["content"]],
                "repair_revision": module_revision("openkb.agent.document_plan_repair_state"),
            }
        )
        self.limits = limits
        self.next_attempt = 0
        self.fingerprint: str | None = None
        self.base_request_hash = content_id([messages[0]["content"], messages[-1]["content"]])
        saved = checkpoints.load_recovery(self.key, "plan_repair")
        if isinstance(saved, dict) and saved.get("base_request_hash") == self.base_request_hash:
            request = saved.get("repair_request")
            attempt = saved.get("next_attempt")
            if type(attempt) is int and attempt >= limits.max_attempts:
                self.next_attempt = attempt
            if isinstance(request, dict) and type(attempt) is int and attempt > 0:
                if content_id(request.get("rejected_candidate")) == request.get("candidate_hash"):
                    rows = [dict(row) for row in messages]
                    payload = json.loads(rows[-1]["content"])
                    payload["repair_request"] = request
                    rows[-1]["content"] = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    )
                    self.messages = WireMessages(rows, messages.identities)
                    self.next_attempt = attempt
                    self.fingerprint = saved.get("fingerprint")
        if self.next_attempt >= limits.max_attempts:
            raise ProcessingIncomplete("document_plan_invalid", "planning")

    def authorize(self, corrected: Any) -> None:
        request = json.loads(self.messages[-1]["content"]).get("repair_request")
        if isinstance(request, dict):
            authorize_repair(request, corrected)

    def invalid(
        self, candidate: Any, error: BaseException, attempt: int
    ) -> tuple[WireMessages, dict[str, Any]]:
        if isinstance(error, RepairScopeError):
            request = json.loads(self.messages[-1]["content"]).get("repair_request")
            authorized = request.get("allowed_changes", []) if isinstance(request, dict) else []
            self.checkpoints.save_recovery(
                self.key,
                "plan_repair",
                {
                    "base_request_hash": self.base_request_hash,
                    "next_attempt": self.limits.max_attempts,
                    "candidate": candidate,
                    "diagnostics": {
                        "issues": [{"code": "repair_scope_violation", "field": error.path}],
                        "allowed_changes": authorized,
                    },
                    "repair_request": request,
                },
            )
            raise ProcessingIncomplete("document_plan_invalid", "planning") from None
        messages, feedback = retry_messages(self.messages, candidate, error, self.decode_kwargs)
        repair_request = json.loads(messages[-1]["content"])["repair_request"]
        fingerprint = content_id(
            sorted((issue["code"], issue["field"]) for issue in feedback["issues"])
            + sorted(feedback["issue_counts"].items())
        )
        terminal = fingerprint == self.fingerprint or any(
            issue.get("allowed_action") == "stop" for issue in feedback["issues"]
        )
        oversized = len(messages[-1]["content"]) > 3 * self.limits.input_capacity
        self.checkpoints.save_recovery(
            self.key,
            "plan_repair",
            {
                "base_request_hash": self.base_request_hash,
                "next_attempt": self.limits.max_attempts if terminal or oversized else attempt + 1,
                "fingerprint": fingerprint,
                "candidate": candidate,
                "repair_request": repair_request,
                "diagnostics": feedback,
            },
        )
        if terminal:
            raise ProcessingIncomplete("document_plan_invalid", "planning") from None
        # A repair needs its original candidate. Do not silently fall back to a
        # fresh unconstrained plan when that candidate cannot fit the suffix.
        if oversized:
            raise ProcessingIncomplete("document_plan_repair_budget_exceeded", "planning")
        self.messages = messages
        self.next_attempt = attempt + 1
        self.fingerprint = fingerprint
        return messages, feedback
