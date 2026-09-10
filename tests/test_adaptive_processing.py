"""Length stops retry real requests and split evidence without publishing fragments."""

import asyncio
import json
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
    return {"processing": values}


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


def test_default_profile_has_no_document_token_ceiling():
    limits = RequestLimits.from_config(DEFAULT_CONFIG)
    assert (limits.context_tokens, limits.output_tokens) == (256 * 1024, 128 * 1024)
    assert (limits.max_context_tokens, limits.max_output_tokens) == (1024 * 1024, 384 * 1024)
    assert limits.max_tokens is None
    budget = ExecutionBudget(RequestLimits.from_config(profile(max_tokens=None)))
    budget.charged_tokens = 50_000_000
    budget.call(lambda **_: response(), model="openai/offline-test", messages=[])
    assert budget.attempts == 1


@pytest.mark.parametrize("stage", ["facts", "planning", "generation", "verification"])
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
        batch = payload.get("units", payload.get("topics", payload.get("facts", [])))
        truncated = payload["stage"] == stage and len(batch) > 1
        calls.append((payload, kwargs["max_tokens"], truncated))
        value = evidence_response(payload)
        if stage == "planning" and payload["stage"] == "facts":
            for item in value["units"]:
                item["facts"][0]["topic"] = item["id"]
        if stage == "planning" and payload["stage"] == "planning":
            value = {
                "topics": [
                    {"name": topic, "title": "Notes", "kind": "concept", "members": [topic]}
                    for topic in payload["topics"]
                ]
            }
        if payload["stage"] == "facts" and not truncated:
            completed.extend(payload["units"])
        return response(value, truncated=truncated)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    failures = [call for call in calls if call[2]]
    assert [call[1] for call in failures[:3]] == [1024, 2048, 3072]
    assert all(call[1] <= 3072 for call in calls)
    assert len(completed) == 3 and len({unit["id"] for unit in completed}) == 3
    assert result.usage["observable_attempts"] == len(calls)
    for page in (kb_dir / "wiki/concepts").glob("*.md"):
        assert '"partial":' not in page.read_text()


def test_finite_request_limit_still_stops_length_retries():
    budget = ExecutionBudget(RequestLimits.from_config(profile(max_requests=1)))
    with pytest.raises(ProcessingIncomplete) as stopped:
        budget.call(lambda **_: response(truncated=True), model="openai/offline-test", messages=[])
    assert stopped.value.reason == "request_budget_exhausted"
    assert budget.attempts == 1


@pytest.mark.parametrize("stage", ["facts", "generation", "verification"])
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
    text = "甲条件 37 kPa；乙条件不能重试。" * 5
    source = tmp_path / "span.md"
    source.write_text(text, encoding="utf-8")
    completed, events = [], []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        evidence = payload.get("units", payload.get("evidence", []))
        truncated = payload["stage"] == stage and any(len(p["text"]) > 24 for p in evidence)
        if payload["stage"] == stage and not truncated:
            completed.extend(evidence)
        return response(evidence_response(payload), truncated=truncated)

    monkeypatch.setattr(litellm, "completion", completion)
    with progress_reporting(events.append):
        result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(completed) > 1
    assert "".join(part["text"] for part in completed) == text
    cursor = 0
    for part in completed:
        assert part["reference"]["start"] == cursor
        cursor += len(part["text"])
        assert part["reference"]["end"] == cursor
    for phase in ("facts", "generation"):
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
    assert result.knowledge_compilation == "unfinished"
    assert result.reason == "output_budget_exhausted"
    assert len(calls) == 1  # Legacy profile has no adaptive ceiling override.
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_split_span_keeps_assets_headings_and_exact_neighbor_locations():
    from openkb.agent.evidence_retry import split_units

    unit = {
        "id": "old",
        "text": "甲乙丙丁戊己",
        "reference": {"block_id": "block", "start": 15, "end": 21},
        "span": {"block": "block", "start": 15, "end": 21, "total": 80},
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
        {"max_output_tokens": 1000},
        {"max_output_tokens": 1024 * 1024},
        {"max_tokens": 0},
        {"max_tokens": False},
    ],
)
def test_invalid_limits_rejected_before_dispatch(changes):
    with pytest.raises(ProcessingIncomplete):
        RequestLimits.from_config(profile(**changes))


def test_larger_candidate_can_use_context_ceiling_before_verification(monkeypatch):
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 5000)
    budget = ExecutionBudget(
        RequestLimits.from_config(
            profile(
                context_tokens=4096,
                output_tokens=1024,
                max_context_tokens=16384,
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
    assert calls[0]["max_tokens"] == 2048
    assert budget.attempts == 1 and budget.limits.context_tokens == 8192


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
            "length" if payload["stage"] == "facts" and len(payload["units"]) > 1 else "stop"
        )
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert [call["max_tokens"] for call in model_service[:3]] == [131072, 262144, 393216]
    assert all(call["max_tokens"] <= 393216 for call in model_service)
    assert result.usage["charged_tokens"] == 130 * len(model_service)
    assert result.usage["unknown_usage"] == 0
