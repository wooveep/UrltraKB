"""Bind actual gateway callbacks to one durable execution and response store."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator

from ultrakb.llm_usage import UsageRequest, UsageScope, _field, _now, normalize_usage
from ultrakb.mutation import RecoveryRequired

from .database import DatabaseIdentity
from .execution_store import Execution, ExecutionStore
from .receipt_store import CapturedResponse, ReceiptStore
from .stage_context import parent_stage
from .usage_store import UsageStore

logger = logging.getLogger(__name__)


def _receipt_fields(
    response, *, state, observation, terminal, send_status, business_state
) -> dict[str, Any]:
    fields = {
        key: value
        for key, value in normalize_usage(
            _field(response, "usage") if observation == "transport" else None
        ).items()
        if key != "usage_status" and value is not None
    }
    provider = _field(response, "id")
    if isinstance(provider, str):
        fields["provider_request_id"] = provider
    if terminal:
        fields.update(state=state, ended_at=_now())
    if observation == "transport" and response is not None:
        send_status = "sent"
    fields.update(
        {
            key: value
            for key, value in {
                "observation": observation,
                "send_status": send_status,
                "business_state": business_state,
            }.items()
            if value is not None
        }
    )
    return fields


class StoredModelUsage:
    def __init__(
        self,
        root: Path,
        identity: DatabaseIdentity,
        execution: Execution,
        *,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.root, self.identity, self.execution = root, identity, execution
        self.usage = UsageStore(root, identity)
        self.executions = ExecutionStore(root, identity)
        self.receipts = ReceiptStore(root, identity)
        self.on_event = on_event
        self._guard = RLock()
        self._versions: dict[str, int] = {}
        self._stages: dict[str, str] = {}
        self._stage_ids: dict[str, str] = {}
        self._closed = False
        self.failure: RecoveryRequired | None = None

    @contextmanager
    def activate(self) -> Iterator[UsageScope]:
        from ultrakb.llm_usage import _SCOPE

        scope = UsageScope(self.root, self.execution.execution_id, storage=self)
        token = _SCOPE.set(scope)
        try:
            yield scope
        finally:
            _SCOPE.reset(token)

    def begin(self, request: UsageRequest) -> None:
        with self._guard:
            stage = self._stage_ids.get(request.request_attempt_id)
            if stage is not None:
                self.usage.begin(
                    request,
                    model_budget_id=self.execution.model_budget_id,
                    stage_code="model_request",
                    stage_attempt_id=stage,
                )
                return
            if self.failure is not None:
                raise self.failure
            if self._closed:
                raise RuntimeError("The model execution has finished")
            from .compile_binding import model_work_key

            compile_key = model_work_key(
                self.root, self.identity, self.execution.execution_id, request.stage
            )
            stage = self.executions.start_stage(
                self.execution.execution_id,
                "model_request",
                parent_stage_id=parent_stage(self.root, self.identity, self.execution.execution_id),
                work_key=compile_key or request.logical_call_id or request.request_attempt_id,
                source_revision_id=request.source_revision_id,
                unit_revision_id=request.unit_revision_id,
            )
            try:
                self.usage.begin(
                    request,
                    model_budget_id=self.execution.model_budget_id,
                    stage_code="model_request",
                    stage_attempt_id=stage,
                )
            except BaseException:
                try:
                    self.executions.finish_stage(stage, state="failed")
                except Exception:
                    logger.warning("Could not finish rejected request stage", exc_info=True)
                raise
            self._versions[request.request_attempt_id] = 0
            self._stages[request.request_attempt_id] = stage
            self._stage_ids[request.request_attempt_id] = stage
        self._report_progress()

    def _version(self, request_id: str) -> int:
        if request_id not in self._versions:
            raise ValueError("A request callback belongs to another execution")
        self._versions[request_id] += 1
        return self._versions[request_id]

    def _retained(self, result: CapturedResponse) -> None:
        if result.requires_repair:
            self.failure = RecoveryRequired(
                "The receipt is retained; repair its pending record before continuing"
            )
            raise self.failure

    def sending(self, request_id: str) -> None:
        """The transport can start only after its durable send intent succeeds."""
        with self._guard:
            if self._closed:
                raise RuntimeError("The model execution has finished")
            if self.failure is not None:
                raise self.failure
            try:
                self.usage.merge(request_id, version=self._version(request_id), send_status="sent")
            except (sqlite3.Error, OSError) as error:
                result = self.receipts.capture_usage(
                    request_id,
                    version=self._version(request_id),
                    usage={"state": "failed", "send_status": "not_sent", "ended_at": _now()},
                )
                self._retained(result)
                self.failure = RecoveryRequired(
                    "The pre-send receipt failed; no transport was started"
                )
                raise self.failure from error
        self._report_progress()

    def finish(
        self, request_id, response, *, state, observation, terminal, send_status, business_state
    ):
        fields = _receipt_fields(
            response,
            state=state,
            observation=observation,
            terminal=terminal,
            send_status=send_status,
            business_state=business_state,
        )
        with self._guard:
            self._retained(
                self.receipts.capture_usage(
                    request_id, version=self._version(request_id), usage=fields
                )
            )
            stage = self._stages.get(request_id)
            if terminal and stage is not None:
                try:
                    self.executions.finish_stage(stage, state=state)
                except (sqlite3.Error, OSError):
                    logger.warning("Could not confirm model stage completion", exc_info=True)
                else:
                    del self._stages[request_id]
        if terminal:
            self._report_progress()

    def _report_progress(self):
        if self.on_event:
            try:
                self.on_event(
                    {
                        "event": "model_usage_progress",
                        "data": self.usage.receipt(execution_id=self.execution.execution_id),
                    }
                )
            except Exception:
                logger.warning("Could not deliver model usage progress", exc_info=True)

    def capture(
        self,
        request_id: str,
        response: bytes,
        *,
        observed_response=None,
        state="completed",
        observation=None,
        send_status="sent",
        cache_hit=False,
    ):
        fields = _receipt_fields(
            observed_response if observation == "transport" else None,
            state=state,
            observation=observation,
            terminal=True,
            send_status=send_status,
            business_state=None,
        )
        if cache_hit:
            fields.update(
                application_cache_hit=True,
                input_total=0,
                output_total=0,
                cached_input=0,
                reasoning_output=0,
            )
        with self._guard:
            result = self.receipts.capture(
                request_id, response, version=self._version(request_id), usage=fields
            )
        self._retained(result)

    def close(self) -> None:
        """Revoke new sends after the caller has settled its SDK and worker work."""
        with self._guard:
            self._closed = True
            for request_id, stage in list(self._stages.items()):
                try:
                    if self.usage.request(request_id).state == "started":
                        self.finish(
                            request_id,
                            None,
                            state="failed",
                            observation=None,
                            terminal=True,
                            send_status=None,
                            business_state="failed",
                        )
                except RecoveryRequired:
                    pass  # The durable repair packet owns the unfinished observation.
                except (sqlite3.Error, OSError):
                    logger.warning("Could not confirm the abandoned request", exc_info=True)
                if request_id not in self._stages:
                    continue
                try:
                    self.executions.finish_stage(stage, state="interrupted")
                except (sqlite3.Error, OSError):
                    logger.warning("Model stage completion remains unconfirmed", exc_info=True)
                else:
                    del self._stages[request_id]
            if self.failure is not None:
                raise self.failure
