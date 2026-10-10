"""Reserve one real send and one SDK turn before entering an answer tool loop."""

from __future__ import annotations

from openkb.llm_execution import AnswerReserveReached, ModelBudgetExceeded

FINAL_INSTRUCTION = """Finish the answer now using only evidence already returned.
Tools are disabled. Answer supported parts fully, including conditions, explanations,
calculations and explicit source negatives. Identify necessary parts still unresolved
and the execution limit below. An unread source does not establish absence. Do not
claim exhaustive library coverage. No further tool request or review is possible.
"""


class AnswerFinisher:
    def __init__(self, executor, agent, max_turns: int):
        self.executor, self.agent, self.max_turns = executor, agent, max_turns
        self.turns = 0
        self.reason: str | None = None
        self.returned = False
        self.original_tools = agent.tools
        # Captured once for this question; this margin is not a latency guarantee.
        self.margin_seconds = 15.0
        executor.budget.preserve("answer_finish", 1)

    def prepare(self, *, retry: bool = False):
        self.executor.policy.check()
        if self.returned:
            raise ModelBudgetExceeded("Finalization already returned; tool loop cannot restart")
        if not retry:
            if self.turns >= self.max_turns:
                raise ModelBudgetExceeded("Answer turn limit exhausted")
            self.turns += 1
        if self.reason:
            return
        snapshot = self.executor.budget.snapshot()
        if self.turns >= self.max_turns:
            self.finish("turn_limit")
        elif (
            snapshot["remaining_seconds"] is not None
            and snapshot["remaining_seconds"] <= self.margin_seconds
        ):
            self.finish("deadline")
        else:
            try:
                self.executor.budget.require("answer")
            except AnswerReserveReached:
                self.finish("call_budget")

    def finish(self, reason: str):
        self.executor.policy.check()
        self.reason = self.reason or reason
        # No already-started tool is abandoned: the SDK joins tool results before
        # it asks the managed model for this next response.
        self.agent.tools = []
        self.executor.budget.release("answer_finish")
        self.executor.budget.require("answer")

    def receive(self, output):
        if self.reason:
            if any(getattr(item, "type", "").endswith("_call") for item in output):
                raise ModelBudgetExceeded("Tool request returned during disabled-tool finalization")
            self.returned = True

    def close(self):
        self.executor.budget.release("answer_finish")
        self.agent.tools = self.original_tools

    @property
    def unfinished(self) -> tuple[str, ...]:
        return (f"Unverified required parts after {self.reason}",) if self.reason else ()
