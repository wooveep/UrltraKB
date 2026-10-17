"""Observe the shared model queue without changing admission or retry limits."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypedDict

from ultrakb.execution_timing import record_phase_event, start_observation
from ultrakb.llm_usage import UsageScope, active_scope

if TYPE_CHECKING:
    from ultrakb.llm_execution import CompletionExecutor, ModelRequest


class _Correlation(TypedDict):
    scope: UsageScope | None
    stage: str
    work_id: str
    logical_call_id: str


class ModelSlot:
    def __init__(self, executor: CompletionExecutor, request: ModelRequest, logical_id: str):
        self.executor = executor
        scope = active_scope() or executor.scope
        if scope and executor.scope and scope.kb_dir != executor.scope.kb_dir:
            raise ValueError("Model execution belongs to another knowledge base")
        self.correlation: _Correlation = dict(
            scope=scope,
            stage=request.operation + "." + request.stage,
            work_id=request.stage,
            logical_call_id=logical_id,
        )
        self.started = start_observation()
        record_phase_event("queued", at=self.started, **self.correlation)

    def _acquired(self):
        try:
            self.executor.policy.check()
            self.acquired = start_observation()
            record_phase_event(
                "slot_acquired",
                started=self.started,
                at=self.acquired,
                slot_limit=self.executor.policy.concurrency,
                **self.correlation,
            )
        except BaseException:
            self.executor._slots.release()
            raise
        return self

    def __enter__(self):
        while not self.executor._slots.acquire(timeout=0.05):
            self.executor.policy.check()
        return self._acquired()

    async def __aenter__(self):
        while not self.executor._slots.acquire(blocking=False):
            self.executor.policy.check()
            await asyncio.sleep(0.01)
        return self._acquired()

    def __exit__(self, _type, error, _traceback):
        released = start_observation()
        self.executor._slots.release()
        from ultrakb.llm_usage_transport import _failure_state

        record_phase_event(
            "slot_released",
            started=self.acquired,
            at=released,
            slot_limit=self.executor.policy.concurrency,
            outcome=_failure_state(error) if error else "completed",
            **self.correlation,
        )

    async def __aexit__(self, *args):
        self.__exit__(*args)
