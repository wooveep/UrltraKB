"""Streamed compilation retries silence without overlapping local transports."""

import threading
import time

import pytest

from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
from tests.test_adaptive_processing import profile
from tests.test_compilation_streaming import call, chunk


def test_default_allows_five_timeout_retries():
    assert RequestLimits.from_config(profile()).timeout_retries == 5


def test_existing_complete_profiles_inherit_retries_without_being_rewritten():
    from openkb.application.settings_data import GlobalConfigValues

    values = profile()["processing"]
    del values["timeout_retries"]
    loaded = GlobalConfigValues(processing=values).processing
    assert loaded == values and "timeout_retries" not in loaded
    assert RequestLimits.from_config({"processing": loaded}).timeout_retries == 5


@pytest.mark.parametrize("value", [-1, True, 1.5, "5"])
def test_invalid_retry_setting_is_rejected(value):
    with pytest.raises(ProcessingIncomplete):
        RequestLimits.from_config(profile(timeout_retries=value))


def test_five_timeouts_then_success_retries_exactly_five_times():
    budget = ExecutionBudget(RequestLimits.from_config(profile()))
    attempts = []

    def produce(**kwargs):
        attempts.append(1)
        if len(attempts) <= 5:
            raise TimeoutError("No new content")
        yield chunk("{}", finish="stop")

    assert call(budget, produce).choices[0].message.content == "{}"
    assert len(attempts) == budget.attempts == 6
    assert budget.unknown_usage == 6  # The successful fixture also omits usage.


def test_sixth_timeout_stops_and_latches_the_budget():
    budget = ExecutionBudget(RequestLimits.from_config(profile()))
    attempts = []

    def produce(**kwargs):
        attempts.append(1)
        raise TimeoutError("No new content")

    with pytest.raises(ProcessingIncomplete, match="request_timeout"):
        call(budget, produce)
    with pytest.raises(ProcessingIncomplete):
        call(budget, produce)
    assert len(attempts) == 6


def test_idle_retry_waits_for_the_old_generator_to_close():
    budget = ExecutionBudget(
        RequestLimits.from_config(profile(request_timeout=0.025, cleanup_timeout=0.3))
    )
    active = 0
    peak = 0
    attempts = []

    def produce(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        attempts.append(1)
        try:
            if len(attempts) == 1:
                yield chunk(reasoning="working")
                time.sleep(0.08)
            yield chunk("{}", finish="stop")
        finally:
            active -= 1

    assert call(budget, produce).choices[0].message.content == "{}"
    assert len(attempts) == 2
    assert peak == 1 and active == 0


def test_unreleased_transport_is_not_overlapped_by_a_retry():
    budget = ExecutionBudget(
        RequestLimits.from_config(profile(request_timeout=0.025, cleanup_timeout=0.025))
    )
    release = threading.Event()
    attempts = []

    def produce(**kwargs):
        attempts.append(1)
        release.wait(2)
        yield chunk("{}", finish="stop")

    try:
        with pytest.raises(ProcessingIncomplete, match="request_timeout"):
            call(budget, produce)
        assert len(attempts) == 1
    finally:
        release.set()


@pytest.mark.parametrize("limit", ["stage_timeout", "document_timeout"])
def test_explicit_deadline_prevents_another_timeout_retry(limit):
    budget = ExecutionBudget(RequestLimits.from_config(profile(**{limit: 0.1})))
    attempts = []

    def produce(**kwargs):
        attempts.append(1)
        raise TimeoutError("No new content")

    with pytest.raises(ProcessingIncomplete, match="time_budget_exhausted"):
        call(budget, produce)
    assert len(attempts) == 1


def test_cumulative_request_limit_still_caps_timeout_retries():
    budget = ExecutionBudget(RequestLimits.from_config(profile(max_requests=1)))

    def produce(**kwargs):
        raise TimeoutError("No new content")

    with pytest.raises(ProcessingIncomplete, match="request_budget_exhausted"):
        call(budget, produce)
    assert budget.attempts == 1


def test_real_http_timeout_retries_the_compiler_request(kb_dir, model_service):
    import json

    from openkb.agent.compiler import _llm_call
    from openkb.config import resolve_credential_bundle, resolve_effective_config
    from openkb.processing import processing_scope
    from tests.http_model_fixture import evidence_response

    def respond(body):
        if len(model_service) == 1:
            time.sleep(0.3)
        return evidence_response(json.loads(body["messages"][-1]["content"]))

    model_service.respond = respond
    settings = resolve_effective_config(kb_dir)[0]
    settings["processing"] = {**settings["processing"], "request_timeout": 0.15}
    with processing_scope(settings) as budget:
        result = _llm_call(
            settings["model"],
            [{"role": "user", "content": '{"stage":"planning","topics":["alpha"]}'}],
            "planning",
            bundle=resolve_credential_bundle(kb_dir),
        )
    assert json.loads(result)["topics"][0]["members"] == ["alpha"]
    assert len(model_service) == budget.attempts == 2
    assert budget.unknown_usage == 1
