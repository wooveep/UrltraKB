"""Document work may outgrow 1000 sends without resetting usage or stop controls."""

import threading
from types import SimpleNamespace

import pytest

from ultrakb.execution_budget import TaskBudget
from ultrakb.llm_execution import (
    CompletionExecutor,
    LlmCredentialBundle,
    ModelBudgetExceeded,
    ModelCallPolicy,
    ModelDeadlineExceeded,
    RoleBindings,
    StageBudgetExceeded,
)
from ultrakb.locks import LockCancelled


def executor(**options):
    return CompletionExecutor(
        RoleBindings("unused", LlmCredentialBundle(), model_table={}),
        ModelCallPolicy(**options),
    )


def test_document_work_can_outgrow_1000_sends_with_compilation_reserved():
    budget = TaskBudget(ModelCallPolicy())
    budget.preserve("compile", 3)
    for _ in range(3001):
        budget.sent(budget.acquire("document_index"))
    assert budget.snapshot()["used"] == 3001
    assert budget.snapshot()["limit"] is None
    assert budget.snapshot()["reserved"] == {"compile": 3}
    for _ in range(3):
        budget.sent(budget.acquire("compile"))
    assert budget.snapshot()["used"] == 3004
    assert budget.snapshot()["reserved"] == {"compile": 0}


def test_explicit_null_is_an_unbounded_model_policy():
    from ultrakb.config_validation import validate_model_policy

    options = validate_model_policy({"model_policy": {"max_calls": None}})
    options.pop("deadline_seconds")
    budget = TaskBudget(ModelCallPolicy(**options))
    budget.restore(997, {"document_index": 997})
    budget.preserve("compile", 3)
    budget.require("document_index", 2272)
    assert budget.snapshot()["used"] == 997


def test_explicit_1000_send_limit_still_preserves_compilation_capacity():
    budget = TaskBudget(ModelCallPolicy(max_calls=1000))
    budget.restore(997, {"document_index": 997})
    budget.preserve("compile", 3)
    with pytest.raises(ModelBudgetExceeded, match="997/1000 used, 3 reserved"):
        budget.acquire("document_index")
    for _ in range(3):
        budget.sent(budget.acquire("compile"))
    assert budget.snapshot()["used"] == 1000


def test_unbounded_task_keeps_per_question_retrieval_limits():
    runtime = executor()
    runtime.budget.restore(1001, {"document_index": 1001})
    assert runtime.allow_retrieval_repair()
    assert runtime.allow_retrieval_repair()
    assert not runtime.allow_retrieval_repair()
    for _ in range(8):
        runtime.reserve("knowledge_retrieval")
    with pytest.raises(StageBudgetExceeded):
        runtime.reserve("conversation_retrieval")
    runtime.begin_question()
    runtime.reserve("knowledge_retrieval")
    assert runtime.calls == 1010


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_unbounded_work_still_stops_after_request_admission(monkeypatch, stop):
    now = SimpleNamespace(value=100.0)
    clock = SimpleNamespace(monotonic=lambda: now.value)
    monkeypatch.setattr("ultrakb.llm_execution.time", clock)
    monkeypatch.setattr("ultrakb.execution_budget.time", clock)
    stopped = threading.Event()
    budget = TaskBudget(ModelCallPolicy(deadline=101.0, cancelled=stopped.is_set))
    budget.restore(1001, {"document_index": 1001})
    budget.preserve("compile", 3)
    admitted = budget.acquire("document_index")
    if stop == "cancel":
        stopped.set()
    else:
        now.value = 102.0
    with pytest.raises(LockCancelled if stop == "cancel" else ModelDeadlineExceeded):
        budget.sent(admitted)
    assert budget.snapshot()["used"] == 1001
    assert budget.snapshot()["pending"] == 0
    assert budget.snapshot()["reserved"] == {"compile": 3}


def test_removing_call_limit_keeps_paid_index_request_identity():
    from ultrakb.index_llm import PageIndexLLM

    before = PageIndexLLM(executor(max_calls=1000))
    after = PageIndexLLM(executor())
    messages = [{"role": "user", "content": "Same title and original page context"}]
    assert before.executor.fingerprint != after.executor.fingerprint
    assert before._key(messages, "verification") == after._key(messages, "verification")


def test_unbounded_budget_receipt_is_readable_and_retains_actual_sends():
    from ultrakb.llm_usage import describe_model_budget, is_model_budget_description

    lines = describe_model_budget(
        {"model_tasks": [{"task_id": "a" * 32, "actual_sends": 1007, "limit": None}]}
    )
    assert all(is_model_budget_description(line) for line in lines)
    assert "1007" in lines[1] and "无固定上限" in lines[1]
    assert "None" not in lines[1]
