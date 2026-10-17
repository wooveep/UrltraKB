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


def test_legacy_997_send_task_resumes_without_cap_or_lost_consumption(kb_dir):
    from ultrakb.model_task import TaskJournal, task_path

    original = executor(max_calls=1000)
    original.budget.restore(997, {"document_index": 997})
    first = TaskJournal(kb_dir, original)
    first.bind("a" * 32, "b" * 32)
    original_path = task_path(kb_dir, first.record.task_id)
    original_bytes = original_path.read_bytes()

    resumed = executor()
    second = TaskJournal(kb_dir, resumed, resume=first.record.task_id)
    assert resumed.calls == second.record.used == 997
    assert second.record.parent_task_id == first.record.task_id
    assert second.record.policy["max_calls"] is None
    assert second.record.inputs == first.record.inputs
    resumed.budget.preserve("compile", 3)
    for _ in range(10):
        resumed.reserve("document_index")
    assert resumed.calls == second.record.used == 1007
    assert original_path.read_bytes() == original_bytes

    restarted = executor()
    third = TaskJournal(kb_dir, restarted, resume=second.record.task_id)
    assert third.record.task_id == second.record.task_id
    assert restarted.calls == 1007
    restarted.reserve("compile")
    assert third.record.used == 1008


def test_finite_limit_cannot_silently_replace_an_unbounded_retained_task(kb_dir):
    from ultrakb.model_task import TaskJournal

    first = TaskJournal(kb_dir, executor())
    with pytest.raises(ValueError, match="Task policy changed"):
        TaskJournal(kb_dir, executor(max_calls=1000), resume=first.record.task_id)


def test_default_execution_resumes_legacy_limit_and_reports_unbounded_usage(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import active_scope, describe_model_budget
    from ultrakb.llm_usage_execution import model_task_receipts
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    config = kb_dir / ".ultrakb/config.yaml"
    original_config = "model: openai/gpt-4o-mini\nlanguage: en\n"
    atomic_write_text(config, original_config + "model_policy:\n  max_calls: 1000\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), first.begin(kb_dir):
        first.executor.budget.restore(996, {"document_index": 996})
        first.executor.reserve("document_index")
    assert first.model_task.record.used == 997

    atomic_write_text(config, original_config)
    second = ExecutionContext(resume_model_task_id=first.model_task.record.task_id)
    with kb_ingest_lock(kb_dir / ".ultrakb"), second.begin(kb_dir):
        assert second.executor.policy.max_calls is None
        assert second.executor.calls == 997
        second.executor.budget.preserve("compile", 3)
        for _ in range(4):
            second.executor.reserve("document_index")
        tasks = model_task_receipts(kb_dir, [active_scope().execution_id])
        assert tasks[0]["actual_sends"] == 1001
        assert tasks[0]["limit"] is None
        assert "无固定上限" in describe_model_budget({"model_tasks": tasks})[1]


def test_removing_call_limit_keeps_paid_index_request_identity():
    from ultrakb.index_llm import PageIndexLLM

    before = PageIndexLLM(executor(max_calls=1000))
    after = PageIndexLLM(executor())
    messages = [{"role": "user", "content": "Same title and original page context"}]
    assert before.executor.fingerprint != after.executor.fingerprint
    assert before._key(messages, "verification") == after._key(messages, "verification")


def test_compilation_can_follow_indexing_beyond_the_old_limit():
    from ultrakb.application.documents import _run_compile_with_retry

    runtime = executor()
    runtime.budget.restore(1001, {"document_index": 1001})

    async def compile_remaining():
        runtime.reserve("compile")

    with runtime.activate():
        _run_compile_with_retry(compile_remaining, "remaining compilation", report=lambda _: None)
    assert runtime.calls == 1002


def test_unbounded_budget_receipt_is_readable_and_retains_actual_sends():
    from ultrakb.llm_usage import describe_model_budget, is_model_budget_description

    lines = describe_model_budget(
        {"model_tasks": [{"task_id": "a" * 32, "actual_sends": 1007, "limit": None}]}
    )
    assert all(is_model_budget_description(line) for line in lines)
    assert "1007" in lines[1] and "无固定上限" in lines[1]
    assert "None" not in lines[1]
