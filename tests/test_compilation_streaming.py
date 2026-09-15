"""Compile real streamed model responses without mistaking active reasoning for a stall."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
from tests.test_adaptive_processing import profile


def chunk(text=None, *, reasoning=None, finish=None, usage=None):
    return SimpleNamespace(
        id="stream-fixture",
        model="offline-test",
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(content=text, reasoning_content=reasoning),
                finish_reason=finish,
            )
        ],
        usage=usage,
    )


def call(budget, produce):
    return budget.call(
        produce,
        model="openai/offline-test",
        messages=[{"role": "user", "content": "Plan"}],
        stream=True,
    )


def test_active_reasoning_outlives_timeout_and_records_activity_without_text(tmp_path):
    from openkb.runtime.diagnostics import WorkerDiagnostics, install_llm_diagnostics

    budget = ExecutionBudget(RequestLimits.from_config(profile(request_timeout=0.3)))
    events = []

    def produce(**kwargs):
        for _ in range(6):
            yield chunk(reasoning="PRIVATE REASONING")
            time.sleep(0.07)
        yield chunk('{"topics":[]}')
        yield chunk(
            finish="stop",
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=20,
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=17,
                ),
            ),
        )

    sdk = SimpleNamespace(completion=produce, acompletion=None)
    with WorkerDiagnostics(tmp_path / "stream.log", events.append):
        install_llm_diagnostics(sdk)
        result = call(budget, sdk.completion)
    assert result.choices[0].message.content == '{"topics":[]}'
    assert budget.observations[0]["usage"] == {"input": 3, "output": 20}
    measured = budget.measurement.value["requests"][0]
    assert measured["request_seconds"] > 0.3
    assert measured["response_activity"]["reasoning_characters"] == 6 * len("PRIVATE REASONING")
    assert measured["response_activity"]["content_characters"] == len('{"topics":[]}')
    assert measured["reasoning_tokens"] == 17
    assert "PRIVATE REASONING" not in json.dumps(budget.measurement.value)
    assert "PRIVATE REASONING" not in (tmp_path / "stream.log").read_text()
    assert any("推理" in event.get("text", "") for event in events)


@pytest.mark.parametrize("mode", ["silent", "empty_chunks", "reasoning_then_stall"])
def test_only_meaningful_output_resets_idle_wait_and_no_automatic_retry(mode):
    budget = ExecutionBudget(
        RequestLimits.from_config(profile(request_timeout=0.15, timeout_retries=0))
    )
    release = threading.Event()
    sent = []

    def produce(**kwargs):
        sent.append(1)
        if mode == "reasoning_then_stall":
            yield chunk(reasoning="x")
        if mode == "empty_chunks":
            for _ in range(10):
                if release.wait(0.03):
                    break
                yield chunk()
        else:
            release.wait(2)
        yield chunk("{}", finish="stop")

    try:
        with pytest.raises(ProcessingIncomplete, match="request_timeout"):
            call(budget, produce)
        with pytest.raises(ProcessingIncomplete):
            call(budget, produce)
        assert len(sent) == 1
        assert budget.unknown_usage == 1
        activity = budget.measurement.value["requests"][0]["response_activity"]
        assert activity["reasoning_characters"] == (1 if mode == "reasoning_then_stall" else 0)
    finally:
        release.set()


def test_explicit_document_deadline_still_stops_active_stream():
    budget = ExecutionBudget(
        RequestLimits.from_config(
            profile(
                request_timeout=1,
                document_timeout=0.15,
            )
        )
    )
    release = threading.Event()

    def produce(**kwargs):
        while not release.wait(0.02):
            yield chunk(reasoning="x")

    try:
        with pytest.raises(ProcessingIncomplete, match="time_budget_exhausted"):
            call(budget, produce)
    finally:
        release.set()


def test_eof_without_finish_does_not_accept_partial_json():
    budget = ExecutionBudget(RequestLimits.from_config(profile()))
    with pytest.raises(ProcessingIncomplete, match="request_outcome_unknown"):
        call(budget, lambda **kwargs: iter([chunk('{"topics":[]}')]))


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 3},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {
            "prompt_tokens": 7,
            "completion_tokens": 9,
            "total_tokens": 16,
            "completion_tokens_details": {"reasoning_tokens": 6},
        },
    ],
)
def test_real_sdk_stream_preserves_usage_and_compiler_text(kb_dir, model_service, usage):
    from openkb.agent.compiler import _llm_call
    from openkb.config import resolve_credential_bundle, resolve_effective_config
    from openkb.execution_measurement import validate_measurement
    from openkb.processing import processing_scope

    model_service.usage = usage
    settings = resolve_effective_config(kb_dir)[0]
    with processing_scope(settings) as budget:
        result = _llm_call(
            settings["model"],
            [{"role": "user", "content": '{"stage":"planning","topics":["alpha"]}'}],
            "planning",
            bundle=resolve_credential_bundle(kb_dir),
        )
    assert json.loads(result)["topics"][0]["members"] == ["alpha"]
    assert model_service[0]["stream"] is True
    assert model_service[0]["stream_options"]["include_usage"] is True
    expected = (
        {"input": usage["prompt_tokens"], "output": usage["completion_tokens"]}
        if usage and "completion_tokens" in usage
        else None
    )
    assert budget.observations[0]["usage"] == expected
    assert budget.unknown_usage == (1 if expected is None else 0)
    validate_measurement(budget.measurement.value)


def test_real_sdk_synthetic_stop_cannot_hide_a_disconnected_stream(kb_dir, model_service):
    from openkb.agent.compiler import _llm_call
    from openkb.config import resolve_credential_bundle, resolve_effective_config
    from openkb.processing import processing_scope

    model_service.stream_disconnect = True
    settings = resolve_effective_config(kb_dir)[0]
    with (
        processing_scope(settings) as budget,
        pytest.raises(ProcessingIncomplete, match="request_outcome_unknown"),
    ):
        _llm_call(
            settings["model"],
            [{"role": "user", "content": "Plan"}],
            "planning",
            bundle=resolve_credential_bundle(kb_dir),
        )
    assert len(model_service) == 1
    assert budget.unknown_usage == 1
