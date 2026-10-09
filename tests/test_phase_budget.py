"""One task allowance admits work without counting reservations as model sends."""

import asyncio

import pytest
from test_llm_execution import executor_for, request
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("limit", [5, 10, 100, 1000])
def test_index_cannot_spend_compilation_reservation(model_service, limit):
    from openkb.llm_execution import ModelBudgetExceeded

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


@pytest.mark.asyncio
async def test_parallel_sends_retries_and_release_share_one_limit(model_service):
    from openkb.llm_execution import ModelBudgetExceeded

    model_service.replies.extend([(503, {"error": {"message": "busy"}}), (200, response())] * 4)
    executor = executor_for(model_service, max_calls=5, retries=1, backoff=0, concurrency=5)
    executor.budget.preserve("compile", 2)
    values = await asyncio.gather(
        *(executor.acomplete("document_index", request()) for _ in range(5)),
        return_exceptions=True,
    )
    assert any(isinstance(value, ModelBudgetExceeded) for value in values)
    assert executor.calls == len(model_service.requests) == 3
    executor.budget.release("compile")
    assert executor.calls == 3
    assert executor.budget.snapshot()["pending"] == 0
    executor.reserve("compile")
    executor.reserve("compile")
    assert executor.calls == 5


def test_rejected_unsent_request_releases_admission(model_service, monkeypatch):
    import litellm

    executor = executor_for(model_service, max_calls=1)

    def reject(**kwargs):
        raise ValueError("unsupported generation option")

    monkeypatch.setattr(litellm, "completion", reject)
    with pytest.raises(ValueError):
        executor.complete("compile", request())
    assert executor.calls == 0
    assert executor.budget.snapshot()["pending"] == 0


@pytest.mark.asyncio
async def test_compile_plan_is_admitted_before_any_page_generation(model_service, kb_dir):
    import json

    from openkb.agent.compiler import compile_short_doc
    from openkb.llm_execution import ModelBudgetExceeded

    source = kb_dir / "wiki/sources/gardening.md"
    source.write_text("# Gardening\nRoses need pruning. Herbs need drainage.")
    for content in (
        {"content": "Gardening overview", "description": "Gardening"},
        {"create": [{"name": f"plant-{i}", "title": f"Plant {i}"} for i in range(3)]},
    ):
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(content)}))
        )
    executor = executor_for(model_service, max_calls=4, retries=0)
    with executor.activate(), pytest.raises(ModelBudgetExceeded, match="compile"):
        await compile_short_doc("gardening", source, kb_dir, "openai/gpt-4o-mini")
    assert len(model_service.requests) == 2
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_resumed_task_retains_consumption_and_explicit_limit_expansion(kb_dir, model_service):
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    config = kb_dir / ".openkb/config.yaml"
    config.write_text("model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 5\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        first.executor.reserve("document_index")
        first.executor.reserve("compile")
        first.bind_model_input("a" * 32, "b" * 32)
    identity = first.model_task.record.task_id
    second = ExecutionContext(resume_model_task_id=identity)
    with kb_ingest_lock(kb_dir / ".openkb"), second.begin(kb_dir):
        assert second.executor.calls == 2
        assert second.model_task.record.task_id == identity
        second.executor.reserve("compile")
    config.write_text("model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 10\n")
    expanded = ExecutionContext(resume_model_task_id=identity)
    with kb_ingest_lock(kb_dir / ".openkb"), expanded.begin(kb_dir):
        assert expanded.executor.calls == 3
        assert expanded.executor.policy.max_calls == 10
        assert expanded.model_task.record.parent_task_id == identity
        assert expanded.model_task.record.task_id != identity
        assert expanded.model_task.record.inputs == {"a" * 32: "b" * 32}


def test_restarting_same_task_cannot_revive_expired_deadline(kb_dir):
    import time

    from openkb.application.execution import ExecutionContext
    from openkb.llm_execution import ModelDeadlineExceeded
    from openkb.locks import atomic_write_json, kb_ingest_lock
    from openkb.model_task import task_path

    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        first.executor.reserve("compile")
    retained = first.model_task.record.model_copy(update={"deadline_at": time.time() - 1})
    atomic_write_json(task_path(kb_dir, retained.task_id), retained.model_dump())
    resumed = ExecutionContext(resume_model_task_id=retained.task_id)
    with kb_ingest_lock(kb_dir / ".openkb"), pytest.raises(ModelDeadlineExceeded):
        with resumed.begin(kb_dir):
            pytest.fail("Expired task was admitted")
    assert resumed.executor.calls == 1
