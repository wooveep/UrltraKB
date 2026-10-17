"""Atomic admission, phase capacity and actual sends for one logical model task."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable


class TaskBudget:
    def __init__(self, policy):
        self.policy = policy
        self._guard = threading.RLock()
        self._used = 0
        self._stages: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._pending: dict[str, str] = {}
        self.on_change: Callable[[dict], None] = lambda snapshot: None

    @staticmethod
    def _stage(role):
        if role in {"knowledge_retrieval", "conversation_retrieval"}:
            return "retrieval", 8
        return ("conversation_index", 6) if role == "conversation_index" else (role, None)

    def snapshot(self) -> dict:
        with self._guard:
            return {
                "used": self._used,
                "limit": self.policy.max_calls,
                "pending": len(self._pending),
                "reserved": dict(self._reserved),
                "stages": dict(self._stages),
                "remaining_seconds": (
                    max(0, self.policy.deadline - time.monotonic())
                    if self.policy.deadline is not None
                    else None
                ),
            }

    def _require(self, role: str, minimum: int):
        from ultrakb.llm_execution import (
            AnswerReserveReached,
            ModelBudgetExceeded,
            StageBudgetExceeded,
        )

        self.policy.check()
        held = sum(count for name, count in self._reserved.items() if name != role)
        if self.policy.max_calls is not None:
            remaining = self.policy.max_calls - self._used - len(self._pending) - held
            if minimum > remaining:
                if self._reserved.get("answer_finish") and minimum <= remaining + held:
                    raise AnswerReserveReached(
                        "Final-answer send is reserved; stop new model tools"
                    )
                raise ModelBudgetExceeded(
                    f"{role} needs at least {minimum} sends; {remaining} available "
                    f"({self._used}/{self.policy.max_calls} used, {held} reserved). "
                    "Retained input and completed checkpoints can be resumed."
                )
        stage, limit = self._stage(role)
        pending = sum(self._stage(value)[0] == stage for value in self._pending.values())
        if limit is not None and self._stages.get(stage, 0) + pending + minimum > limit:
            raise StageBudgetExceeded(f"{stage} stage send allowance exhausted")

    def require(self, role: str, minimum: int = 1):
        with self._guard:
            self._require(role, minimum)

    def preserve(self, role: str, minimum: int):
        """Capacity is owned by a phase and consumed only by its actual sends."""
        if type(minimum) is not int or minimum < 0:
            raise ValueError("Phase capacity must be a nonnegative integer")
        with self._guard:
            self._require(role, minimum)
            self._reserved[role] = minimum

    def release(self, role: str):
        with self._guard:
            self._reserved.pop(role, None)

    def acquire(self, role: str) -> str:
        with self._guard:
            self._require(role, 1)
            identity = uuid.uuid4().hex
            self._pending[identity] = role
            return identity

    def sent(self, identity: str):
        """Commit immediately before transport, including each transport retry."""
        with self._guard:
            role = self._pending.pop(identity)
            self._require(role, 1)
            self._used += 1
            stage, _ = self._stage(role)
            self._stages[stage] = self._stages.get(stage, 0) + 1
            if self._reserved.get(role, 0):
                self._reserved[role] -= 1
            self.on_change(self.snapshot())

    def abandon(self, identity: str):
        with self._guard:
            self._pending.pop(identity, None)

    def reset_question(self):
        with self._guard:
            if self._pending:
                raise RuntimeError("Cannot reset question allowances while requests are pending")
            for stage in ("retrieval", "conversation_index"):
                self._stages.pop(stage, None)

    def restore(self, used: int, stages: dict[str, int]):
        if (
            type(used) is not int
            or used < 0
            or any(type(value) is not int or value < 0 for value in stages.values())
        ):
            raise ValueError("Invalid retained task consumption")
        with self._guard:
            if self._used or self._pending:
                raise RuntimeError("Task consumption can only be restored before execution")
            self._used, self._stages = used, dict(stages)


def admit_compile_plan(plan, *, rewrite_summary: bool, missing_creates: int | None = None):
    from ultrakb.llm_execution import active_executor

    executor = active_executor()
    if executor is None:
        return
    # Updates admit their actual missing proposals after sizing and checkpoint validation.
    count = (
        sum(len(group.create) for group in (plan.concepts, plan.entities))
        if missing_creates is None
        else missing_creates
    )
    minimum = count
    repairs = min(count, executor.policy.compile_repair_calls)
    executor.budget.require("compile", minimum + repairs)


def admit_document_summary(batch_count: int) -> None:
    """All basic summary sends and one plan must fit before sending any batch."""
    from ultrakb.llm_execution import active_executor

    if executor := active_executor():
        executor.budget.require("compile", batch_count + 1)
