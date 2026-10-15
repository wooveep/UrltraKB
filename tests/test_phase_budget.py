"""One task allowance admits work without counting reservations as model sends."""

import pytest
from test_llm_execution import executor_for

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("limit", [5, 10, 100, 1000])
def test_index_cannot_spend_compilation_reservation(model_service, limit):
    from ultrakb.llm_execution import ModelBudgetExceeded

    executor = executor_for(model_service, max_calls=limit, retries=0)
    executor.budget.preserve("compile", 3)
    assert executor.calls == 0
    for _ in range(limit - 3):
        executor.reserve("document_index")
    with pytest.raises(ModelBudgetExceeded):
        executor.reserve("document_index")
    executor.begin_question()
    assert executor.calls == limit - 3
    for _ in range(3):
        executor.reserve("compile")
    executor.budget.release("compile")
    assert executor.calls == limit
    assert executor.budget.snapshot()["reserved"] == {}


def test_parallel_sends_retries_and_release_share_one_limit():
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.execution_budget import TaskBudget
    from ultrakb.llm_execution import ModelBudgetExceeded, ModelCallPolicy

    budget = TaskBudget(ModelCallPolicy(max_calls=5, retries=1, concurrency=5))
    budget.preserve("compile", 2)

    def admit_send():
        try:
            identity = budget.acquire("document_index")
            budget.sent(identity)
            return identity
        except ModelBudgetExceeded as error:
            return error

    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(pool.map(lambda _: admit_send(), range(5)))
    assert sum(isinstance(value, ModelBudgetExceeded) for value in values) == 2
    assert budget.snapshot()["used"] == 3
    budget.release("compile")
    assert budget.snapshot()["pending"] == 0
    for _ in range(2):
        budget.sent(budget.acquire("compile"))
    assert budget.snapshot()["used"] == 5
    with pytest.raises(ModelBudgetExceeded):
        budget.acquire("document_index")


def test_rejected_unsent_request_releases_admission():
    from ultrakb.execution_budget import TaskBudget
    from ultrakb.llm_execution import ModelCallPolicy

    budget = TaskBudget(ModelCallPolicy(max_calls=1))
    identity = budget.acquire("compile")
    assert budget.snapshot()["pending"] == 1
    budget.abandon(identity)
    assert budget.snapshot()["used"] == budget.snapshot()["pending"] == 0
    budget.sent(budget.acquire("compile"))
    assert budget.snapshot()["used"] == 1


def test_compile_plan_is_admitted_before_any_page_generation(model_service, kb_dir):
    from ultrakb.agent.compile_plan import CompilePlan, PlanGroup
    from ultrakb.execution_budget import admit_compile_plan
    from ultrakb.llm_execution import ModelBudgetExceeded

    plan = CompilePlan(
        concepts=PlanGroup(
            create=[{"name": f"plant-{i}", "title": f"Plant {i}"} for i in range(3)]
        ),
        entities=PlanGroup(),
    )
    executor = executor_for(model_service, max_calls=4, retries=0)
    executor.reserve("compile")
    executor.reserve("compile")
    with executor.activate(), pytest.raises(ModelBudgetExceeded, match="compile"):
        admit_compile_plan(plan, rewrite_summary=False)
    assert executor.calls == 2
    assert len(model_service.requests) == 0
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_resumed_task_retains_consumption_and_explicit_limit_expansion(kb_dir, model_service):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text("model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 5\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), first.begin(kb_dir):
        first.executor.reserve("document_index")
        first.executor.reserve("compile")
        first.bind_model_input("a" * 32, "b" * 32)
    identity = first.model_task.record.task_id
    second = ExecutionContext(resume_model_task_id=identity)
    with kb_ingest_lock(kb_dir / ".ultrakb"), second.begin(kb_dir):
        assert second.executor.calls == 2
        assert second.model_task.record.task_id == identity
        second.executor.reserve("compile")
    config.write_text("model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 10\n")
    expanded = ExecutionContext(resume_model_task_id=identity)
    with kb_ingest_lock(kb_dir / ".ultrakb"), expanded.begin(kb_dir):
        assert expanded.executor.calls == 3
        assert expanded.executor.policy.max_calls == 10
        assert expanded.model_task.record.parent_task_id == identity
        assert expanded.model_task.record.task_id != identity
        assert expanded.model_task.record.inputs == {"a" * 32: "b" * 32}


def test_restarting_same_task_cannot_revive_expired_deadline(kb_dir):
    import time

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_execution import ModelDeadlineExceeded
    from ultrakb.locks import atomic_write_json, kb_ingest_lock
    from ultrakb.model_task import task_path

    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), first.begin(kb_dir):
        first.executor.reserve("compile")
    retained = first.model_task.record.model_copy(update={"deadline_at": time.time() - 1})
    atomic_write_json(task_path(kb_dir, retained.task_id), retained.model_dump())
    resumed = ExecutionContext(resume_model_task_id=retained.task_id)
    with kb_ingest_lock(kb_dir / ".ultrakb"), pytest.raises(ModelDeadlineExceeded):
        with resumed.begin(kb_dir):
            pytest.fail("Expired task was admitted")
    assert resumed.executor.calls == 1
