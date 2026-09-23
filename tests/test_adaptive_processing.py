"""Length stops retry real requests and split evidence without publishing fragments."""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import litellm
import pytest
import yaml

from openkb.config import DEFAULT_CONFIG
from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
from tests.http_model_fixture import evidence_response


def response(value=None, *, truncated=False, tokens=10):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(value) if not truncated else '{"partial":'
                ),
                finish_reason="length" if truncated else "stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=tokens),
    )


def profile(**changes):
    values = deepcopy(DEFAULT_CONFIG["processing"])
    values.update(
        context_tokens=256 * 1024,
        output_tokens=128 * 1024,
        max_context_tokens=1024 * 1024,
        max_output_tokens=384 * 1024,
        max_tokens=20_000_000,
    )
    values.update(changes)
    return {"model": "private/unknown-model", "processing": values}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unknown_transport_stops_subsequent_dispatch_in_the_same_budget(asynchronous):
    budget = ExecutionBudget(RequestLimits.from_config(profile()))
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("Remote result was lost")

    async def acompletion(**kwargs):
        return completion(**kwargs)

    for _ in range(2):
        with pytest.raises(ProcessingIncomplete, match="request_outcome_unknown"):
            kwargs = {
                "model": "openai/offline-test",
                "messages": [{"role": "user", "content": "Facts"}],
            }
            if asynchronous:
                asyncio.run(budget.acall(acompletion, **kwargs))
            else:
                budget.call(completion, **kwargs)
    assert len(calls) == budget.attempts == 1
    assert budget.unknown_usage == 1


def test_request_preparing_during_a_lost_response_cannot_dispatch_after_the_stop(monkeypatch):
    budget = ExecutionBudget(RequestLimits.from_config(profile(concurrency=2)))
    preparing, release = threading.Event(), threading.Event()
    counter = litellm.token_counter
    sent, errors = [], []

    def tokens(*args, **kwargs):
        if kwargs.get("messages", [{}])[0].get("content") == "second":
            preparing.set()
            assert release.wait(5)
        return counter(*args, **kwargs)

    def transport(**kwargs):
        sent.append(kwargs["messages"][0]["content"])
        raise TimeoutError("Remote response was lost")

    def call(label):
        try:
            budget.call(
                transport,
                model="openai/offline-test",
                messages=[{"role": "user", "content": label}],
            )
        except ProcessingIncomplete as exc:
            errors.append(exc.reason)

    monkeypatch.setattr(litellm, "token_counter", tokens)
    worker = threading.Thread(target=call, args=("second",))
    worker.start()
    try:
        assert preparing.wait(5)
        call("first")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert sent == ["first"]
    assert errors == ["request_outcome_unknown", "request_outcome_unknown"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_length_escalates_to_model_capacity_and_counts_all_attempts(asynchronous):
    budget = ExecutionBudget(RequestLimits.from_config(profile()))
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return response(truncated=len(calls) < 3, tokens=kwargs["max_tokens"])

    async def acompletion(**kwargs):
        return completion(**kwargs)

    kwargs = {"model": "openai/offline-test", "messages": [{"role": "user", "content": "Facts"}]}
    result = (
        asyncio.run(budget.acall(acompletion, **kwargs))
        if asynchronous
        else budget.call(completion, **kwargs)
    )
    assert result.choices[0].finish_reason == "stop"
    assert [call["max_tokens"] for call in calls] == [128 * 1024, 256 * 1024, 384 * 1024]
    assert budget.limits.context_tokens == 1024 * 1024
    assert budget.attempts == 3 and budget.unknown_usage == 0
    assert budget.charged_tokens == sum(call["max_tokens"] + 10 for call in calls)
    budget.checkpoint("planning")


def test_default_profile_uses_the_selected_model_contract_not_a_generic_ceiling(monkeypatch):
    from openkb.model_capabilities import ModelCapabilities

    monkeypatch.setattr(
        "openkb.processing_limits.selected_model_capabilities",
        lambda *_: ModelCapabilities(1_000_000, 900_000, 300_000, False),
    )
    limits = RequestLimits.from_config(DEFAULT_CONFIG)
    assert (limits.context_tokens, limits.input_capacity) == (1_000_000, 900_000)
    assert limits.output_tokens == DEFAULT_CONFIG["processing"]["output_tokens"]
    assert limits.max_output_tokens == 300_000
    assert "context_tokens" not in DEFAULT_CONFIG["processing"]
    assert limits.max_tokens is None
    monkeypatch.undo()
    budget = ExecutionBudget(
        RequestLimits.from_config(profile(max_tokens=None, max_output_tokens=300_000))
    )
    budget.charged_tokens = 50_000_000
    budget.call(lambda **_: response(), model="openai/offline-test", messages=[])
    assert budget.attempts == 1


def test_unknown_model_accepts_an_explicit_independent_input_output_contract():
    limits = RequestLimits.from_config(
        {
            "model": "private/unknown-model",
            "processing": {
                **DEFAULT_CONFIG["processing"],
                "input_tokens": 12_000,
                "max_input_tokens": 24_000,
                "shared_context": False,
                "output_tokens": 2_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    assert (limits.input_capacity, limits.max_input_tokens) == (12_000, 24_000)
    assert (limits.output_tokens, limits.max_output_tokens, limits.shared_context) == (
        2_000,
        4_000,
        False,
    )


def test_unknown_independent_capacity_requires_an_explicit_output_ceiling():
    with pytest.raises(ProcessingIncomplete, match="model_capabilities_required"):
        RequestLimits.from_config(
            {
                "model": "private/unknown-model",
                "processing": {
                    **DEFAULT_CONFIG["processing"],
                    "input_tokens": 12_000,
                    "max_input_tokens": 24_000,
                    "shared_context": False,
                },
            }
        )


def test_unknown_model_requires_an_explicit_shared_or_independent_contract():
    with pytest.raises(ProcessingIncomplete, match="model_capabilities_required"):
        RequestLimits.from_config(
            {"model": "private/unknown-model", "processing": DEFAULT_CONFIG["processing"]}
        )


def test_discovered_model_respects_an_explicit_output_ceiling(monkeypatch):
    from openkb.model_capabilities import ModelCapabilities

    monkeypatch.setattr(
        "openkb.processing_limits.selected_model_capabilities",
        lambda *_: ModelCapabilities(1_000_000, 900_000, 300_000, False),
    )
    limits = RequestLimits.from_config(
        {
            "model": "known-model",
            "processing": {**DEFAULT_CONFIG["processing"], "max_output_tokens": 2_048},
        }
    )
    assert (limits.output_tokens, limits.max_output_tokens) == (2_048, 2_048)


def test_explicit_shared_context_keeps_the_discovered_output_ceiling(monkeypatch):
    from openkb.model_capabilities import ModelCapabilities

    monkeypatch.setattr(
        "openkb.processing_limits.selected_model_capabilities",
        lambda *_: ModelCapabilities(1_000_000, 900_000, 300_000, False),
    )
    processing = {
        **DEFAULT_CONFIG["processing"],
        "context_tokens": 100_000,
        "max_context_tokens": 200_000,
    }
    limits = RequestLimits.from_config({"model": "known-model", "processing": processing})

    # The user's W/S/T envelope stays shared and bounded, but the default
    # reservation can still grow toward the endpoint's independently known
    # completion capability instead of being mistaken for its maximum.
    assert (limits.context_tokens, limits.max_context_tokens, limits.shared_context) == (
        100_000,
        200_000,
        True,
    )
    assert limits.output_tokens == DEFAULT_CONFIG["processing"]["output_tokens"]
    assert limits.max_output_tokens == 300_000
    while (expanded := limits.expanded()) != limits:
        limits = expanded
    assert (limits.context_tokens, limits.output_tokens) == (200_000, 199_999)

    with pytest.raises(ProcessingIncomplete, match="model_capabilities_required"):
        RequestLimits.from_config(
            {
                "model": "known-model",
                "processing": {**processing, "max_context_tokens": 1_000_001},
            }
        )
    with pytest.raises(ProcessingIncomplete, match="model_capabilities_required"):
        RequestLimits.from_config(
            {
                "model": "known-model",
                "processing": {
                    **DEFAULT_CONFIG["processing"],
                    "max_output_tokens": 300_001,
                },
            }
        )


@pytest.mark.parametrize(
    "contract",
    [
        {"context_tokens": 100_000, "max_context_tokens": 100_000},
        {"input_tokens": 100_000, "max_input_tokens": 100_000, "shared_context": False},
    ],
)
def test_explicit_capacity_contract_clamps_the_output_reservation(contract):
    limits = RequestLimits.from_config(
        {
            "model": "private/unknown-model",
            "processing": {
                **DEFAULT_CONFIG["processing"],
                **contract,
                "output_tokens": 16_384,
                "max_output_tokens": 4_096,
            },
        }
    )
    assert (limits.output_tokens, limits.max_output_tokens) == (4_096, 4_096)


@pytest.mark.parametrize("stage", ["generation", "verification"])
def test_compilation_splits_at_capacity_and_publishes_only_complete_batches(
    kb_dir, tmp_path, monkeypatch, stage
):
    from openkb.application.documents import import_document

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"] = profile(
        context_tokens=8192, output_tokens=1024, max_context_tokens=32768, max_output_tokens=3072
    )["processing"]
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "adaptive.md"
    source.write_text("Alpha condition.\n\nBeta condition.\n\nGamma condition.")
    calls, completed = [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        batch = payload.get("evidence", {}).get("blocks", [])
        truncated = payload["stage"] == stage and len(batch) > 1
        calls.append((payload, kwargs["max_tokens"], truncated))
        value = evidence_response(payload)
        if payload["stage"] == stage and not truncated:
            completed.extend(batch)
        return response(value, truncated=truncated)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    if stage == "verification":
        # A critical review sees the merged final candidate exactly once.  It
        # must not be silently split into weaker per-fragment reviews.
        assert result.knowledge_compilation == "unfinished"
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
        return
    assert result.knowledge_compilation == "completed", result
    failures = [call for call in calls if call[2]]
    assert [call[1] for call in failures[:3]] == [1024, 2048, 3072]
    assert all(call[1] <= 3072 for call in calls)
    assert len(completed) == 3 and {unit["text"].strip() for unit in completed} == {
        "Alpha condition.",
        "Beta condition.",
        "Gamma condition.",
    }
    assert result.usage["observable_attempts"] == len(calls)
    for page in (kb_dir / "wiki/concepts").glob("*.md"):
        assert '"partial":' not in page.read_text()


def test_finite_request_limit_still_stops_length_retries():
    budget = ExecutionBudget(RequestLimits.from_config(profile(max_requests=1)))
    with pytest.raises(ProcessingIncomplete) as stopped:
        budget.call(lambda **_: response(truncated=True), model="openai/offline-test", messages=[])
    assert stopped.value.reason == "request_budget_exhausted"
    assert budget.attempts == 1


@pytest.mark.parametrize("stage", ["generation", "verification"])
def test_single_long_span_splits_without_gaps_and_progress_counts_only_finished_work(
    kb_dir, tmp_path, monkeypatch, stage
):
    from openkb.application.documents import import_document
    from openkb.progress import progress_reporting

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"] = profile(
        context_tokens=8192, output_tokens=1024, max_context_tokens=32768, max_output_tokens=3072
    )["processing"]
    config_path.write_text(yaml.safe_dump(config))
    text = "\n".join(["甲条件 37 kPa；乙条件不能重试。"] * 5)
    source = tmp_path / "span.md"
    source.write_text(text, encoding="utf-8")
    completed, events = [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        evidence = payload.get("evidence", {}).get("blocks", [])
        truncated = payload["stage"] == stage and any(len(p["text"]) > 24 for p in evidence)
        if payload["stage"] == stage and not truncated:
            completed.extend(evidence)
        return response(evidence_response(payload), truncated=truncated)

    monkeypatch.setattr(litellm, "completion", completion)
    with progress_reporting(events.append):
        result = import_document(kb_dir, source)
    if stage == "verification":
        assert result.knowledge_compilation == "unfinished"
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
        return
    assert result.knowledge_compilation == "completed", result
    assert len(completed) > 1
    assert "".join(part["text"] for part in completed) == text
    cursor = 0
    for part in completed:
        span = part["reference"]
        assert span["start"] == cursor
        cursor += len(part["text"])
        assert span["end"] == cursor
    for phase in ("generation",):
        counters = [
            step for event in events for step in event["progress"] if step["phase"] == phase
        ]
        assert counters[-1]["completed"] == counters[-1]["total"]
        assert all(step["completed"] <= step["total"] for step in counters)


def test_indivisible_truncation_stops_without_publishing(kb_dir, tmp_path, monkeypatch):
    from openkb.application.documents import import_document

    source = tmp_path / "indivisible.md"
    source.write_text("X")
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return response(truncated=True)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.knowledge_compilation == "unfinished"
    assert result.reason == "planning_output_budget_exhausted"
    assert calls
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_split_span_keeps_assets_headings_and_exact_neighbor_locations():
    from openkb.agent.evidence_retry import split_units

    unit = {
        "id": "old",
        "text": "甲乙丙\n丁戊己",
        "reference": {"block_id": "block", "start": 15, "end": 22},
        "span": {"block": "block", "start": 15, "end": 22, "total": 80},
        "assets": ["image-digest"],
        "location": {"page": 3},
        "headings": ["安装"],
        "context": "表格列标题",
        "heading_evidence": [],
        "neighbors": [],
    }
    parts = [part for batch in split_units([unit]) for part in batch]
    assert len({part["id"] for part in parts}) == 2
    assert "".join(part["text"] for part in parts) == unit["text"]
    for part in parts:
        for key in ("assets", "location", "context", "headings", "heading_evidence"):
            assert part[key] == unit[key]
        for neighbor in part["neighbors"]:
            start, end = neighbor["reference"]["start"], neighbor["reference"]["end"]
            assert neighbor["text"] == unit["text"][start - 15 : end - 15]


@pytest.mark.parametrize(
    "changes",
    [
        {"max_context_tokens": 2000},
        {"max_output_tokens": 1024 * 1024},
        {"max_tokens": 0},
        {"max_tokens": False},
    ],
)
def test_invalid_limits_rejected_before_dispatch(changes):
    with pytest.raises(ProcessingIncomplete):
        RequestLimits.from_config(profile(**changes))


@pytest.mark.parametrize("input_tokens", [50_000, 120_000])
def test_larger_candidate_can_use_context_ceiling_before_verification(monkeypatch, input_tokens):
    monkeypatch.setattr(litellm, "token_counter", lambda **_: input_tokens)
    budget = ExecutionBudget(
        RequestLimits.from_config(
            profile(
                context_tokens=32768,
                output_tokens=1024,
                max_context_tokens=131072,
                max_output_tokens=3072,
            )
        )
    )
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return response()

    budget.call(completion, model="openai/offline-test", messages=[])
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1024
    assert budget.attempts == 1
    assert budget.limits.context_tokens == (65536 if input_tokens == 50_000 else 131072)


def test_deepseek_transport_sends_full_limits_and_recovers_from_length(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document

    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"] = "deepseek/deepseek-v4-flash"
    config["processing"] = deepcopy(DEFAULT_CONFIG["processing"])
    path.write_text(yaml.safe_dump(config))
    source = tmp_path / "deepseek.md"
    source.write_text("Alpha condition.\n\nBeta condition.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        model_service.finish_reason = (
            "length"
            if payload["stage"] == "generation" and len(payload["evidence"]["blocks"]) > 1
            else "stop"
        )
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    generation_limits = [
        call["max_tokens"]
        for call in model_service
        if json.loads(call["messages"][-1]["content"])["stage"] == "generation"
    ]
    assert generation_limits[:3] == [16_384, 32_768, 65_536]
    assert all(call["max_tokens"] <= 393_216 for call in model_service)
    assert result.usage["charged_tokens"] == 130 * len(model_service)
    assert result.usage["unknown_usage"] == 0
