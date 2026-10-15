"""Pure task-policy rules; no model response or network dependency."""

import threading
from types import SimpleNamespace

import pytest

from ultrakb.execution_budget import TaskBudget
from ultrakb.llm_execution import (
    AnswerReserveReached,
    ModelBudgetExceeded,
    ModelCallPolicy,
    ModelDeadlineExceeded,
    StageBudgetExceeded,
)
from ultrakb.locks import LockCancelled

pytestmark = pytest.mark.unit


def test_pending_requests_cannot_spend_the_reserved_final_answer_send():
    budget = TaskBudget(ModelCallPolicy(max_calls=3))
    budget.preserve("answer_finish", 1)
    first = budget.acquire("answer")
    second = budget.acquire("answer")
    with pytest.raises(AnswerReserveReached):
        budget.acquire("answer")
    assert budget.snapshot()["pending"] == 2
    assert budget.snapshot()["used"] == 0
    assert budget.snapshot()["reserved"] == {"answer_finish": 1}

    budget.sent(first)
    budget.sent(second)
    with pytest.raises(AnswerReserveReached):
        budget.require("answer")
    budget.release("answer_finish")
    budget.sent(budget.acquire("answer"))
    assert budget.snapshot()["used"] == 3
    assert budget.snapshot()["pending"] == 0
    with pytest.raises(ModelBudgetExceeded):
        budget.acquire("answer")


@pytest.mark.parametrize("stop", ["cancel", "deadline", "both"])
def test_stop_after_admission_never_commits_or_consumes_final_answer_capacity(monkeypatch, stop):
    now = SimpleNamespace(value=100.0)
    clock = SimpleNamespace(monotonic=lambda: now.value)
    monkeypatch.setattr("ultrakb.llm_execution.time", clock)
    monkeypatch.setattr("ultrakb.execution_budget.time", clock)
    stopped = threading.Event()
    budget = TaskBudget(ModelCallPolicy(max_calls=2, deadline=101.0, cancelled=stopped.is_set))
    budget.preserve("answer_finish", 1)
    admission = budget.acquire("answer")
    if stop in {"cancel", "both"}:
        stopped.set()
    if stop in {"deadline", "both"}:
        now.value = 102.0
    expected = ModelDeadlineExceeded if stop == "deadline" else LockCancelled
    with pytest.raises(expected):
        budget.sent(admission)
    assert budget.snapshot()["used"] == budget.snapshot()["pending"] == 0
    assert budget.snapshot()["reserved"] == {"answer_finish": 1}
    with pytest.raises(expected):
        budget.acquire("answer_finish")


def test_question_reset_renews_shared_retrieval_stage_without_reviving_task_sends():
    budget = TaskBudget(ModelCallPolicy(max_calls=11))
    budget.restore(6, {"retrieval": 6})
    budget.preserve("answer_finish", 1)
    knowledge = budget.acquire("knowledge_retrieval")
    conversation = budget.acquire("conversation_retrieval")
    with pytest.raises(StageBudgetExceeded):
        budget.acquire("knowledge_retrieval")
    assert budget.snapshot()["used"] == 6
    assert budget.snapshot()["pending"] == 2
    budget.sent(knowledge)
    budget.sent(conversation)
    assert budget.snapshot()["stages"] == {"retrieval": 8}
    with pytest.raises(StageBudgetExceeded):
        budget.acquire("conversation_retrieval")

    budget.reset_question()
    assert budget.snapshot()["used"] == 8
    assert budget.snapshot()["stages"] == {}
    assert budget.snapshot()["reserved"] == {"answer_finish": 1}
    budget.sent(budget.acquire("conversation_retrieval"))
    budget.sent(budget.acquire("knowledge_retrieval"))
    with pytest.raises(AnswerReserveReached):
        budget.acquire("conversation_retrieval")
    budget.release("answer_finish")
    budget.sent(budget.acquire("answer"))
    assert budget.snapshot()["used"] == 11
    with pytest.raises(ModelBudgetExceeded):
        budget.acquire("answer")
