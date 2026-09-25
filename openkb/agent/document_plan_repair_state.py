"""Bounded, durable repair state for one rejected planning window."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from openkb.agent.document_json_prompts import repair_task_rules
from openkb.agent.document_plan_compiler import PlanningContext, compile_plan_candidate
from openkb.agent.document_plan_feedback import RepairScopeError, authorize_repair, retry_messages
from openkb.agent.document_plan_issues import PlanValidationError, parse_plan_json
from openkb.agent.document_plan_patches import (
    apply_plan_patch,
    compact_patch_request,
    item_refs,
    patch_request,
)
from openkb.agent.document_plan_routing import (
    RoutingDecisionError,
    apply_routing_decisions,
    routing_request,
)
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_protocol import PlanningProjectionRequired
from openkb.agent.document_window_receipts import window_receipt_id
from openkb.agent.evidence_units import JSON_FORMAT
from openkb.agent.evidence_wire import WireMessages, _map
from openkb.agent.model_json import json_text
from openkb.execution_receipt import ModelText
from openkb.implementation import module_revision
from openkb.processing import ProcessingIncomplete
from openkb.processing_limits import InputTooLarge
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


class V3PlanningRepairSession:
    """Private v3 draft and response journal for one planning target."""

    def __init__(
        self,
        messages: WireMessages,
        checkpoints: Any,
        window: dict[str, Any],
        predecessor: str,
        limits: Any,
        block_chars: list[int],
        model: str | None = None,
        context: PlanningContext | None = None,
    ) -> None:
        self.base_messages = messages
        self.messages = messages
        self.checkpoints = checkpoints
        self.limits = limits
        self.block_chars = block_chars
        self.model = model
        self.context = context
        self.plan_protocol = context.selection_protocol if context else "document-plan-v3"
        self.protocol = (
            "document-plan-repair-v2"
            if context is not None
            and context.selection_protocol in {"document-plan-v4", "document-plan-v5"}
            else "document-plan-repair-v1"
        )
        self.schema = (
            f"{self.plan_protocol}-repair-v2"
            if self.protocol == "document-plan-repair-v2"
            else "document-plan-v3-repair-v1"
        )
        self.resolver = (
            SelectionResolver.from_context(context)
            if context is not None and self.protocol == "document-plan-repair-v2"
            else None
        )
        self.target_ranges = list(context.target_ranges or []) if context is not None else []
        self.base_request_hash = content_id([messages[0]["content"], messages[-1]["content"]])
        self.key = content_id(
            {
                "schema": self.schema,
                "input": checkpoints.input,
                "window": window_receipt_id(window),
                "predecessor": predecessor,
                "request": self.base_request_hash,
                "implementation": [
                    module_revision("openkb.agent.document_plan_compiler"),
                    module_revision("openkb.agent.document_plan_diagnostics"),
                    module_revision("openkb.agent.document_plan_patches"),
                    module_revision("openkb.agent.document_range_validation"),
                    module_revision("openkb.agent.document_plan_feedback"),
                    module_revision("openkb.agent.document_plan_selections"),
                    module_revision("openkb.agent.document_plan_routes"),
                    module_revision("openkb.agent.document_plan_routing"),
                ],
            }
        )
        self.next_attempt = 0
        self.phase: str | None = None
        self.candidate: Any = None
        self.refs: dict[str, Any] | None = None
        self.repair_request: dict[str, Any] | None = None
        self.fingerprint: str | None = None
        self.pending_response: Any = None
        self.last_response: Any = None
        self.applied_candidate: Any = None
        self.response_output_tokens: int | None = None
        self.response_checkpoint: str | None = None
        self.original_response: Any = None
        self.normalization: list[dict[str, Any]] = []
        self.derived_changes: list[dict[str, Any]] = []
        self.validated_decisions: dict[str, dict[str, Any]] = {}
        self.response_status: str | None = None
        self.attempts_used = 0
        self.stop_reason: str | None = None
        self.legacy_recovery_key: str | None = None
        saved = checkpoints.load_recovery(self.key, "plan_repair")
        if (
            isinstance(saved, dict)
            and saved.get("schema") == self.schema
            and saved.get("base_request_hash") == self.base_request_hash
        ):
            self.next_attempt = saved.get("next_attempt", 0)
            self.phase = saved.get("phase")
            self.candidate = saved.get("candidate")
            self.refs = saved.get("item_refs")
            self.repair_request = saved.get("repair_request")
            self.fingerprint = saved.get("fingerprint")
            self.response_output_tokens = saved.get("response_output_tokens")
            self.response_checkpoint = saved.get("response_checkpoint")
            self.last_response = saved.get("last_response")
            if (
                isinstance(saved.get("pending_response"), str)
                and saved.get("response_representation") != "wire"
            ):
                raise ProcessingIncomplete("document_plan_incompatible_recovery", "planning")
            self.pending_response = (
                ModelText(
                    saved["pending_response"],
                    self.response_output_tokens,
                    raw_content=saved.get("response_raw_content"),
                    finish_reason=saved.get("response_finish_reason"),
                    representation="wire",
                )
                if isinstance(saved.get("pending_response"), str)
                else None
            )
            self.applied_candidate = (
                saved.get("compiled_candidate") if saved.get("response_applied") else None
            )
            self.original_response = saved.get("original_response")
            self.normalization = saved.get("normalization", [])
            self.derived_changes = saved.get("derived_changes", [])
            self.validated_decisions = saved.get("validated_decisions", {})
            self.response_status = saved.get("response_status")
            self.attempts_used = saved.get("attempts_used", self.next_attempt)
            self.stop_reason = saved.get("stop_reason")
            self.legacy_recovery_key = saved.get("legacy_recovery_key")
            if self.repair_request:
                self.messages = self._with_request(self.repair_request)
        elif self.resolver is not None and context is not None:
            legacy = self._legacy_v2_recovery()
            if legacy is not None:
                old_key, record = legacy
                self.legacy_recovery_key = old_key
                self.candidate = record["candidate"]
                self.refs = record.get("item_refs")
                self.last_response = record.get("last_response")
                self.original_response = record.get("original_response") or self.candidate
                self.attempts_used = max(
                    record.get("attempts_used", 0), record.get("next_attempt", 0), 1
                )
                self.next_attempt = self.attempts_used
                if self.next_attempt < limits.max_attempts:
                    result = compile_plan_candidate(self.candidate, context)
                    if result.issues:
                        self.invalid(self.candidate, result, self.next_attempt - 1)
                    else:
                        self.applied_candidate = self.candidate
                        self.response_status = "applied"
                        self._save(response_applied=True, compiled_candidate=self.candidate)
        if self.stop_reason or self.next_attempt >= limits.max_attempts:
            reason = (
                self.stop_reason
                or (
                    "document_plan_empty_response"
                    if self.response_status == "response_empty"
                    else "document_plan_invalid"
                )
            )
            raise ProcessingIncomplete(reason, "planning")

    def _legacy_v2_recovery(self) -> tuple[str, dict[str, Any]] | None:
        if self.schema != "document-plan-v4-repair-v2":
            return None
        root = getattr(self.checkpoints, "root", None)
        if not isinstance(root, Path):
            return None
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for path in (root / "recovery").glob("*-plan_repair.json"):
            key = path.name.removesuffix("-plan_repair.json")
            if key == self.key:
                continue
            record = self.checkpoints.load_recovery(key, "plan_repair")
            if (
                not isinstance(record, dict)
                or record.get("schema") != "document-plan-v4-repair-v2"
                or record.get("base_request_hash") != self.base_request_hash
                or not isinstance(record.get("candidate"), dict)
            ):
                continue
            request = record.get("repair_request")
            if (
                not isinstance(request, dict)
                or request.get("repair_protocol") != "document-plan-repair-v2"
            ):
                continue
            expected = content_id(
                {
                    "plan_protocol": "document-plan-v4",
                    "repair_protocol": "document-plan-repair-v2",
                    "candidate": record["candidate"],
                }
            )
            if request.get("candidate_hash") != expected:
                continue
            attempts = record.get("attempts_used", 0)
            next_attempt = record.get("next_attempt", 0)
            if (
                type(attempts) is int
                and type(next_attempt) is int
                and attempts >= 0
                and next_attempt >= 0
            ):
                candidates.append((max(attempts, next_attempt), key, record))
        if not candidates:
            return None
        _, key, record = max(candidates, key=lambda row: (row[0], row[1]))
        return key, record

    def _with_request(self, request: dict[str, Any]) -> WireMessages:
        rows = [dict(row) for row in self.base_messages]
        payload = json.loads(rows[-1]["content"])
        syntax = self.phase == "syntax_repair"
        routing = self.phase == "routing_repair"
        payload["plan_protocol"] = (
            self.plan_protocol
            if syntax and self.resolver
            else request["repair_protocol"]
            if routing
            else self.protocol
        )
        payload["response_mode"] = "plan" if syntax else "routing" if routing else "patch"
        payload["task_rules"] = repair_task_rules(str(self.phase))
        visible = request
        if routing and isinstance(request.get("pending_decision_ids"), list):
            visible = {
                **request,
                "items": [
                    item
                    for item in request["items"]
                    if item["decision_id"] in request["pending_decision_ids"]
                ],
            }
        payload["repair_request"] = _map(visible, self.base_messages.identities)
        rows[-1]["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return WireMessages(rows, self.base_messages.identities)

    def _fits(self, messages: WireMessages) -> bool:
        if self.model is None or not hasattr(self.limits, "request"):
            return len(messages[-1]["content"]) <= 3 * self.limits.input_capacity
        try:
            self.limits.request(self.model, messages, {"response_format": JSON_FORMAT})
        except InputTooLarge:
            return False
        return True

    def _save(self, **extra: Any) -> None:
        receipt = self.pending_response if self.pending_response is not None else self.last_response
        record = {
            "schema": self.schema,
            "base_request_hash": self.base_request_hash,
            "next_attempt": self.next_attempt,
            "attempts_used": self.attempts_used,
            "response_status": self.response_status,
            "stop_reason": self.stop_reason,
            "legacy_recovery_key": self.legacy_recovery_key,
            "phase": self.phase,
            "candidate": (
                str(self.candidate) if isinstance(self.candidate, ModelText) else self.candidate
            ),
            "item_refs": self.refs,
            "repair_request": self.repair_request,
            "fingerprint": self.fingerprint,
            "pending_response": (
                str(self.pending_response)
                if isinstance(self.pending_response, ModelText)
                else self.pending_response
            ),
            "last_response": (
                str(self.last_response)
                if isinstance(self.last_response, ModelText)
                else self.last_response
            ),
            "response_output_tokens": self.response_output_tokens,
            "response_representation": getattr(receipt, "representation", None),
            "response_raw_content": getattr(receipt, "raw_content", None),
            "response_finish_reason": getattr(receipt, "finish_reason", None),
            "response_checkpoint": self.response_checkpoint,
            "original_response": (
                str(self.original_response)
                if isinstance(self.original_response, ModelText)
                else self.original_response
            ),
            "normalization": self.normalization,
            "derived_changes": self.derived_changes,
            "validated_decisions": self.validated_decisions,
            **extra,
        }
        self.checkpoints.save_recovery(self.key, "plan_repair", record)

    def invalid(self, raw: Any, result: Any, attempt: int) -> dict[str, Any]:
        """Persist a recoverable draft before its next model dispatch."""
        issues = result.issues
        if result.candidate is None:
            if not issues or any(
                issue.code not in {"json_syntax", "json_duplicate_field"} for issue in issues
            ):
                self.stop("unrecoverable_syntax")
            error = PlanValidationError("Document plan syntax", list(issues))
            messages, feedback = retry_messages(
                self.base_messages,
                raw,
                error,
                {
                    "total_blocks": len(self.block_chars),
                    "block_chars": self.block_chars,
                    "ignored_blocks": set(),
                },
            )
            request = json.loads(messages[-1]["content"])["repair_request"]
            phase = "syntax_repair"
            candidate = raw
            refs = None
            fingerprint = content_id([(issue.code, issue.path) for issue in issues])
        else:
            candidate = result.candidate
            if not isinstance(candidate, dict):
                self.candidate = candidate
                self.stop(
                    "no_authorized_repair",
                    diagnostics={
                        "issues": [{"code": issue.code, "path": issue.path} for issue in issues]
                    },
                )
            refs = item_refs(candidate, self.refs if isinstance(self.refs, dict) else None)
            routing = (
                self.resolver is not None
                and result.coverage_status == "checked"
                and bool(issues)
                and all(
                    issue.code in {"source_only_conflict", "coverage_gap", "coverage_pending"}
                    for issue in issues
                )
            )
            request = (
                routing_request(candidate, refs, issues, self.context)
                if routing
                else patch_request(
                    candidate,
                    refs,
                    issues,
                    protocol=self.protocol,
                    editable_page_refs=refs["sections"]["page_changes"],
                    target_ranges=self.target_ranges,
                )
            )
            if not (request["items"] if routing else request["allowed_operations"]):
                self.candidate, self.refs = candidate, refs
                self.stop(
                    "no_authorized_repair",
                    diagnostics={"issues": request.get("issues", [])},
                    unassigned_ranges=list(result.unassigned),
                )
            phase = "routing_repair" if routing else "field_repair"
            fingerprint = (
                request["scope_hash"]
                if routing
                else content_id(
                    sorted(
                        content_id(
                            (row["code"], row["item_ref"], row["field"], row["source_ranges"])
                        )
                        for row in request["issues"]
                    )
                )
            )
            diagnostics = (
                [diagnostic for item in request["items"] for diagnostic in item["diagnostics"]]
                if routing
                else request["issues"]
            )
            feedback = {"issues": diagnostics, "all_issues": diagnostics}
        if fingerprint == self.fingerprint:
            self.candidate, self.refs = candidate, refs
            self.stop(
                "no_progress",
                diagnostics=feedback,
                unassigned_ranges=list(result.unassigned),
            )
        self.phase, self.candidate, self.refs = phase, candidate, refs
        self.repair_request = request
        self.fingerprint = fingerprint
        self.next_attempt = attempt + 1
        self.attempts_used = max(self.attempts_used, attempt + 1)
        self.pending_response = None
        self.applied_candidate = None
        self.response_output_tokens = None
        self.response_checkpoint = None
        self.normalization = []
        self.derived_changes = []
        self.validated_decisions = {}
        self.response_status = "rejected"
        if self.original_response is None:
            self.original_response = raw
        self.messages = self._with_request(request)
        if not self._fits(self.messages) and phase == "field_repair":
            for limit in (8, 4, 2, 1):
                projected = compact_patch_request(request, issue_limit=limit)
                messages = self._with_request(projected)
                if self._fits(messages):
                    self.repair_request, self.messages = projected, messages
                    feedback["issues"] = projected["issues"]
                    break
        if not self._fits(self.messages):
            self.stop(
                "document_plan_repair_budget_exceeded",
                diagnostics=feedback,
                unassigned_ranges=list(result.unassigned),
            )
        self._save(diagnostics=feedback, unassigned_ranges=list(result.unassigned))
        return feedback

    def evaluate(
        self, value: Any, context: PlanningContext, attempt: int
    ) -> tuple[Any, dict[str, Any] | None]:
        self.attempts_used = max(self.attempts_used, attempt + 1)
        result = compile_plan_candidate(value, context)
        if not result.issues:
            self.normalization.extend(result.normalizations)
            return result, None
        for issue in result.issues:
            if issue.code != "projection_required":
                continue
            if issue.path.endswith(".target"):
                raise PlanningProjectionRequired(catalog_target=issue.actual)
            if issue.path.endswith(".target_key"):
                raise PlanningProjectionRequired(page_key=issue.actual)
            if ".affected_pages[" in issue.path:
                raise PlanningProjectionRequired(page_key=issue.actual)
            if issue.path.endswith(".unresolved_key"):
                raise PlanningProjectionRequired(unresolved_key=issue.actual)
        return result, self.invalid(value, result, attempt)

    def record_response(self, response: Any, request_key: str) -> None:
        if isinstance(response, str) and not isinstance(response, ModelText):
            response = ModelText(
                response, None, raw_content=response, finish_reason="stop", representation="wire"
            )
        self.pending_response = response
        self.last_response = response
        self.response_output_tokens = getattr(response, "output_tokens", None)
        self.response_checkpoint = request_key
        self.response_status = "received"
        self.attempts_used = max(self.attempts_used, self.next_attempt + 1)
        self._save(response_applied=False)

    def empty_response(self, response: Any, attempt: int) -> None:
        """Consume a completed empty answer without replacing the current repair phase."""
        self.pending_response = None
        self.last_response = response
        self.applied_candidate = None
        self.response_status = "response_empty"
        self.attempts_used = max(self.attempts_used, attempt + 1)
        self.next_attempt = attempt + 1
        if self.next_attempt >= self.limits.max_attempts:
            self.stop("document_plan_empty_response")
        self._save(response_applied=False)

    def rejected(self, error: RepairScopeError, attempt: int) -> None:
        """Keep the same baseline and give precise bounded feedback for a bad patch."""
        recoverable = {
            "invalid_selection_shape",
            "unknown_block_reference",
            "selection_outside_evidence",
            "route_would_empty_page",
            "unknown_repair_decision",
            "repair_decision_missing",
            "repair_decision_conflict",
            "repair_destination_out_of_scope",
            "repair_piece_out_of_scope",
        }
        self.response_status = "rejected"
        self.pending_response = None
        self.applied_candidate = None
        self.attempts_used = max(self.attempts_used, attempt + 1)
        feedback = {
            "code": error.code,
            "path": error.path,
            "operation_index": error.operation_index,
            "item_ref": error.item_ref,
            "field": error.field,
            "grant": error.grant,
            "actual": error.actual,
            "allowed": error.allowed,
        }
        if isinstance(error, RoutingDecisionError):
            feedback["decision_ids"] = error.decision_ids
            self.validated_decisions = error.valid_decisions
        if error.code not in recoverable or attempt + 1 >= self.limits.max_attempts:
            self.stop(
                "repair_deferred" if error.code == "repair_deferred" else "repair_scope_violation",
                rejected_patch=str(self.last_response),
                diagnostics=feedback,
            )
        assert self.repair_request is not None
        if self.phase == "routing_repair" and isinstance(error, RoutingDecisionError):
            self.repair_request["pending_decision_ids"] = [
                item["decision_id"]
                for item in self.repair_request["items"]
                if item["decision_id"] not in self.validated_decisions
            ]
        self.repair_request = {**self.repair_request, "rejected_response": feedback}
        self.next_attempt = attempt + 1
        self.messages = self._with_request(self.repair_request)
        if not self._fits(self.messages):
            self.stop("document_plan_repair_budget_exceeded", diagnostics=feedback)
        self._save(response_applied=False, diagnostics=feedback)

    def replay_receipt(self) -> ModelText:
        return ModelText("", self.response_output_tokens)

    def apply_response(self, response: Any) -> Any:
        if self.repair_request is None:
            raise RepairScopeError("No authorized repair request")
        if self.phase == "syntax_repair":
            authorize_repair(self.repair_request, response)
            return response
        if self.phase == "routing_repair":
            if not isinstance(self.candidate, dict) or self.refs is None or self.context is None:
                raise RepairScopeError("Invalid routing repair phase")
            try:
                parsed = (
                    parse_plan_json(json_text(response))
                    if isinstance(response, (str, bytes))
                    else response
                )
            except (TypeError, ValueError) as exc:
                raise RoutingDecisionError(
                    "Malformed routing response", code="invalid_selection_shape"
                ) from exc
            routed = apply_routing_decisions(
                self.candidate,
                self.refs,
                self.repair_request,
                parsed,
                self.context,
                preserved=self.validated_decisions,
            )
            self.refs = routed.refs
            self.normalization = list(routed.normalizations)
            self.derived_changes = list(routed.derived_changes)
            self.validated_decisions = {}
            return routed.candidate
        if (
            self.phase != "field_repair"
            or not isinstance(self.candidate, dict)
            or self.refs is None
        ):
            raise RepairScopeError("Invalid repair phase")
        try:
            parsed = (
                parse_plan_json(json_text(response))
                if isinstance(response, (str, bytes))
                else response
            )
        except (TypeError, ValueError) as exc:
            raise RepairScopeError(
                "Malformed patch response", code="invalid_selection_shape"
            ) from exc
        if isinstance(parsed, dict) and parsed.get("operations") == []:
            self.stop("no_progress", diagnostics={"issues": self.repair_request.get("issues", [])})
        repaired = apply_plan_patch(
            self.candidate,
            self.refs,
            self.repair_request,
            parsed,
            block_chars=self.block_chars,
            resolver=self.resolver,
        )
        self.refs = repaired.refs
        normalized_response = deepcopy(parsed)
        for row in repaired.normalizations:
            operation = normalized_response["operations"][row["operation_index"]]
            if row["conversion"] == "content_to_value":
                operation["value"] = operation.pop("content")
            elif row["conversion"] == "related_issue_to_grant":
                operation["issue_id"] = row["grant_issue_id"]
        self.normalization = [
            {**row, "normalized_response_hash": content_id(normalized_response)}
            for row in repaired.normalizations
        ]
        self.derived_changes = list(repaired.derived_changes)
        return repaired.candidate

    def applied(self, candidate: Any) -> None:
        self.pending_response = None
        self.candidate = candidate
        self.applied_candidate = candidate
        self.response_status = "applied"
        self._save(response_applied=True, compiled_candidate=candidate)

    def accepted(self, candidate: Any, delta: dict[str, Any]) -> None:
        self.pending_response = None
        self.candidate = candidate
        self.response_status = "applied"
        self._save(
            response_applied=True,
            compiled_candidate=candidate,
            final_candidate_hash=content_id(candidate),
            delta_hash=content_id(delta),
            original_candidate_hash=content_id(self.original_response),
            repair_response_hash=content_id(self.last_response)
            if self.last_response is not None
            else None,
            accepted=True,
        )

    def stop(self, reason: str, **details: Any) -> None:
        self.stop_reason = reason
        if self.schema == "document-plan-v3-repair-v1":
            self.next_attempt = self.limits.max_attempts
        self._save(stop_reason=reason, **details)
        raise ProcessingIncomplete(
            reason
            if reason in {"document_plan_repair_budget_exceeded", "document_plan_empty_response"}
            else "document_plan_invalid",
            "planning",
        )


def planning_context(
    source: Any,
    parsed: Any,
    window: dict[str, Any],
    evidence: dict[str, Any],
    view: Any,
    ledger: Any,
    decode_kwargs: dict[str, Any],
    visible_targets: set[str],
    prior_overview_ranges: list[Any],
) -> PlanningContext:
    return PlanningContext(
        source_version=source.id,
        parse_identity=parsed.id,
        target_receipt_identity=window_receipt_id(window),
        evidence=evidence,
        parsed=parsed,
        target_start=decode_kwargs["target_start"],
        target_end=decode_kwargs["target_end"],
        total_blocks=decode_kwargs["total_blocks"],
        allowed_entity_types=decode_kwargs["allowed_entity_types"],
        existing_targets=visible_targets,
        carry_pages=view.page_register,
        open_unresolved=view.open_references,
        block_chars=decode_kwargs["block_chars"],
        ignored_blocks=decode_kwargs["ignored_blocks"],
        target_ranges=decode_kwargs["target_ranges"],
        evidence_ranges=decode_kwargs["evidence_ranges"],
        prior_overview_ranges=prior_overview_ranges,
        known_page_keys=ledger.page_keys(),
        known_page_name_keys=ledger.page_names(),
        reserved_targets=ledger.catalog_targets(),
        known_unresolved_keys=ledger.unresolved_keys(),
        known_open_unresolved_keys=ledger.unresolved_keys(status="open"),
        selection_protocol=json.loads(view.messages[-1]["content"]).get(
            "plan_protocol", "numeric-v3"
        ),
        navigation_hints=json.loads(view.messages[-1]["content"])
        .get("navigation", {})
        .get("hints", []),
    )
