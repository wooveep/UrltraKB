"""Bounded model admission with durable queue and slot boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ultrakb.execution_timing import phase_observation

if TYPE_CHECKING:
    from ultrakb.llm_execution import CompletionExecutor, ModelRequest


class ModelSlot:
    def __init__(self, executor: CompletionExecutor, request: ModelRequest, logical_id: str):
        self.executor = executor
        self.stage = request.operation + "." + request.stage
        self.logical_id = logical_id
        self.held = False

    def _observation(self, event):
        return phase_observation(event, stage=self.stage, work_id=self.logical_id)

    def _acquired(self):
        self.executor.policy.check()
        self._slot = self._observation("slot")
        self._slot.__enter__()
        return self

    def __enter__(self):
        try:
            with self._observation("queue"):
                while not self.executor._slots.acquire(timeout=0.05):
                    self.executor.policy.check()
                self.held = True
            return self._acquired()
        except BaseException:
            if self.held:
                self.executor._slots.release()
                self.held = False
            raise

    async def __aenter__(self):
        try:
            with self._observation("queue"):
                while not self.executor._slots.acquire(blocking=False):
                    self.executor.policy.check()
                    await asyncio.sleep(0.01)
                self.held = True
            return self._acquired()
        except BaseException:
            if self.held:
                self.executor._slots.release()
                self.held = False
            raise

    def __exit__(self, error_type, error, traceback):
        self.executor._slots.release()
        self.held = False
        self._slot.__exit__(error_type, error, traceback)

    async def __aexit__(self, *args):
        self.__exit__(*args)
