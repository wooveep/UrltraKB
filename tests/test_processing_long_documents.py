"""Default request bounds must not act as a whole-document budget."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from openkb.processing import (
    DEFAULT_PROCESSING,
    ExecutionBudget,
    ProcessingIncomplete,
    RequestLimits,
)


def test_default_planning_keeps_working_after_thirty_minutes(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("openkb.processing.time.monotonic", lambda: now[0])
    budget = ExecutionBudget(
        RequestLimits.from_config({"processing": DEFAULT_PROCESSING}),
        started=0,
        stage_started=0,
        stage="planning",
    )
    for batch in range(33):
        now[0] = batch * 60
        budget.checkpoint("planning")


def test_default_document_can_finish_more_than_two_hundred_requests_after_an_hour(monkeypatch):
    monkeypatch.setattr("openkb.processing.time.monotonic", lambda: 3700)
    monkeypatch.setattr("litellm.token_counter", lambda **kwargs: 1)
    budget = ExecutionBudget(
        RequestLimits.from_config({"processing": DEFAULT_PROCESSING}),
        started=0,
        stage_started=0,
        stage="generation",
    )
    for _ in range(201):
        options, observation = budget.reserve({"model": "openai/offline", "messages": []})
        assert options["timeout"] == 180
        budget.settle(
            observation,
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                choices=[SimpleNamespace(finish_reason="stop")],
            ),
        )


@pytest.mark.parametrize("field", ["stage_timeout", "document_timeout"])
def test_explicit_time_caps_remain_binding(field, monkeypatch):
    monkeypatch.setattr("openkb.processing.time.monotonic", lambda: 11)
    limits = replace(RequestLimits.from_config({"processing": DEFAULT_PROCESSING}), **{field: 10})
    budget = ExecutionBudget(limits, started=0, stage_started=0, stage="planning")
    with pytest.raises(ProcessingIncomplete, match="time_budget_exhausted"):
        budget.checkpoint()


def test_parent_supervisor_uses_the_same_optional_deadlines(monkeypatch):
    from openkb.runtime.tasks import TaskManager

    monkeypatch.setattr("openkb.runtime.tasks.time.monotonic", lambda: 3700)
    attempt = SimpleNamespace(
        limits=RequestLimits.from_config({"processing": DEFAULT_PROCESSING}),
        process=SimpleNamespace(is_alive=lambda: True),
        recovery=False,
        started=0,
        stage_started=0,
        result=None,
        stopping_at=None,
        budget_expired=False,
    )
    task = SimpleNamespace(view=SimpleNamespace(stop_requested=False))
    TaskManager._supervise(None, task, attempt)
    assert not attempt.budget_expired
    attempt.limits = replace(attempt.limits, stage_timeout=1800)
    TaskManager._supervise(None, task, attempt)
    assert attempt.budget_expired


def test_secondary_requests_support_an_unlimited_cumulative_request_budget():
    from openkb.processing import external_request_usage, processing_scope

    with processing_scope({"processing": DEFAULT_PROCESSING}) as budget:
        with external_request_usage(100) as receipt:
            receipt["usage"] = {"input": 1, "output": 1}
        assert budget.attempts == 1
