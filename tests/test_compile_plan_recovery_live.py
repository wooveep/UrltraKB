"""Tiny genuine Flash checks for the actual plan repair/persistence entry.

Run only with the shared live budget. No planner/model return is fabricated, and
the accepted empty plan deliberately avoids page generation or wiki publication.
"""

import json

import pytest
from compile_work_fixtures import new_compile_work
from native_usage_fixtures import observe_executor, read_requests

from ultrakb.agent.compile_plan import CompilePlanError, request_compile_plan
from ultrakb.agent.page_response import PageResponseError
from ultrakb.compilation_report import collect_compile_report
from ultrakb.compile_checkpoints import digest
from ultrakb.config import resolve_credential_bundle
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings

pytestmark = pytest.mark.live


def plan_runtime(kb_dir, live_llm, monkeypatch, *, reject_correction):
    live_llm.install(monkeypatch)
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", resolve_credential_bundle(kb_dir)),
        ModelCallPolicy(max_calls=2, retries=0, backoff=0),
    )
    checkpoints = new_compile_work(kb_dir, {"document": "tiny-plan"}, {"protocol": "plan-v1"})
    observe_executor(kb_dir, executor, work=checkpoints)
    mode = (
        'Always return exactly {"concepts":42}, including after correction requests. '
        "This is an intentionally invalid protocol delivery fixture."
        if reject_correction
        else 'On the initial request return exactly {"concepts":42}. '
        "When the user gives a correction, comply with it and return exactly "
        '{"concepts":{},"entities":{}}.'
    )
    delivered = []
    corrections = []

    def request(correction):
        corrections.append(correction)
        messages = [
            {
                "role": "system",
                "content": "Return only the requested JSON protocol fixture. " + mode,
            },
            {"role": "user", "content": "Return the initial fixture JSON now."},
        ]
        if correction is not None:
            messages.append({"role": "user", "content": correction})
        result = executor.complete(
            "compile",
            ModelRequest(
                messages,
                operation="compile",
                stage="concepts-plan",
                generation_options={
                    "max_tokens": 128,
                    "reasoning_effort": "none",
                    "response_format": {"type": "json_object"},
                },
            ),
        )
        assert result.raw_usage_available
        assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens > 0
        delivered.append(result.text)
        return result.text

    signature = digest(["tiny-plan-v1", mode])

    def receive():
        with executor.activate():
            return request_compile_plan(
                request,
                json.loads,
                checkpoints=checkpoints,
                request_digest=signature,
                existing={"concepts": set(), "entities": set()},
                entity_types=frozenset({"product", "other"}),
                sanitize=str.lower,
            )

    return executor, checkpoints, signature, delivered, corrections, receive


def test_real_plan_repairs_once_and_recovers_the_accepted_payload_without_a_send(
    kb_dir, live_llm, monkeypatch
):
    before = len(live_llm.calls)
    executor, checkpoints, signature, delivered, corrections, receive = plan_runtime(
        kb_dir, live_llm, monkeypatch, reject_correction=False
    )
    with collect_compile_report() as report:
        plan = receive()
    assert json.loads(delivered[0]) == {"concepts": 42}
    corrected = json.loads(delivered[1])
    assert set(corrected) == {"concepts", "entities"}
    for group in corrected.values():
        assert isinstance(group, dict) and set(group) <= {"create", "update", "related"}
        assert all(isinstance(actions, list) and not actions for actions in group.values())
    for group in (plan.concepts, plan.entities):
        assert group.create == group.update == group.related == []
    assert corrections[0] is None and "previous JSON plan failed validation" in corrections[1]
    assert len(delivered) == len(live_llm.calls) - before == executor.calls == 2
    assert checkpoints.load("plan", signature) == delivered[1]
    assert checkpoints.failure_code("plan", signature) is None
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        checkpoints.require_generation("plan", signature)
    assert "compile_plan_repaired" in report.quality and not report.unfinished
    recovered = receive()
    assert recovered == plan
    assert len(delivered) == len(live_llm.calls) - before == 2
    assert executor.scope.storage.executions.budget(checkpoints.model_budget_id).used == 2
    records = read_requests(kb_dir)
    assert len(records) == 2 and all(record.usage_status == "known" for record in records)


def test_two_real_invalid_plans_persist_failure_and_refuse_another_repair(
    kb_dir, live_llm, monkeypatch
):
    before = len(live_llm.calls)
    executor, checkpoints, signature, delivered, corrections, receive = plan_runtime(
        kb_dir, live_llm, monkeypatch, reject_correction=True
    )
    with pytest.raises(CompilePlanError) as caught:
        receive()
    assert caught.value.code == "malformed_plan_items"
    assert [json.loads(value) for value in delivered] == [{"concepts": 42}, {"concepts": 42}]
    assert corrections[0] is None and "previous JSON plan failed validation" in corrections[1]
    assert len(delivered) == len(live_llm.calls) - before == executor.calls == 2
    assert checkpoints.load("plan", signature) is None
    assert checkpoints.failure_code("plan", signature) == "malformed_plan_items"
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        receive()
    assert len(delivered) == len(live_llm.calls) - before == 2
    assert executor.scope.storage.executions.budget(checkpoints.model_budget_id).used == 2
    assert len(read_requests(kb_dir)) == 2
