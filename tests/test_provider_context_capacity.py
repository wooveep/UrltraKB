"""Provider capacity refusals preserve source intake and resumable compilation."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.config import DEFAULT_CONFIG
from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("asynchronous", [False, True])
def test_capacity_refusal_keeps_budget_available_for_a_smaller_request(asynchronous):
    limits = RequestLimits.from_config(
        {
            "processing": {
                **DEFAULT_CONFIG["processing"],
                "context_tokens": 8192,
                "max_context_tokens": 32768,
                "output_tokens": 1024,
                "max_output_tokens": 4096,
                "max_requests": 2,
            }
        }
    )
    budget = ExecutionBudget(limits)
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        if kwargs["messages"][0]["content"] == "Large original context.":
            raise litellm.ContextWindowExceededError(
                message="This model's maximum context length is 8192 tokens.",
                model="offline-test",
                llm_provider="openai",
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    def dispatch(content):
        kwargs = {
            "model": "openai/offline-test",
            "messages": [{"role": "user", "content": content}],
        }
        return (
            asyncio.run(budget.acall(acompletion, **kwargs))
            if asynchronous
            else budget.call(completion, **kwargs)
        )

    with pytest.raises(ProcessingIncomplete, match="provider_context_exceeded"):
        dispatch("Large original context.")
    assert len(calls) == budget.attempts == budget.unknown_usage == 1
    assert budget.limits == limits
    assert budget.charged_tokens == budget.observations[0]["reserved_tokens"]
    assert dispatch("Smaller context.").choices[0].finish_reason == "stop"
    assert len(calls) == budget.attempts == 2
    assert budget.unknown_usage == 1
    assert [call["max_tokens"] for call in calls] == [1024, 1024]
    assert budget.charged_tokens == budget.observations[0]["reserved_tokens"] + 30
    with pytest.raises(ProcessingIncomplete, match="request_budget_exhausted"):
        dispatch("A third request still exceeds the hard allowance.")
    assert len(calls) == 2


def _refuse_context_requests(monkeypatch, refuse):
    calls = []
    transport_request = httpx.HTTPTransport.handle_request

    def provider_request(transport, request):
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            body = json.loads(request.read())
            payload = json.loads(body["messages"][-1]["content"])
            rejected = refuse(payload)
            calls.append((body, payload, rejected))
            if rejected:
                # The actual provider tokenizer sees more input than the local
                # estimator; even zero completion tokens cannot fit this input.
                return httpx.Response(
                    400,
                    request=request,
                    json={
                        "error": {
                            "message": (
                                "This model's maximum context length is 32768 tokens. "
                                "However, you requested 41024 tokens "
                                "(40000 in the messages, 1024 in the completion). "
                                "Please reduce the length of the messages or completion."
                            ),
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "invalid_request_error",
                        }
                    },
                )
        return transport_request(transport, request)

    # Substitute only the external HTTP boundary. Successful requests still
    # traverse the real SDK and the shared local streaming model service.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", provider_request)
    return calls


def test_import_splits_provider_context_refusal_and_continue_reuses_completed_work(
    kb_dir, tmp_path, model_service, monkeypatch
):
    source = tmp_path / "provider-capacity.md"
    paragraphs = ["Alpha condition.", "Beta condition.", "Gamma condition."]
    source.write_text("\n\n".join(paragraphs))
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(
        max_context_tokens=131072,
        max_output_tokens=4096,
        max_requests=40,
        max_tokens=1000000,
        concurrency=1,
    )
    config_path.write_text(yaml.safe_dump(config))
    calls = _refuse_context_requests(
        monkeypatch,
        lambda payload: payload["stage"] == "verification" and len(payload["facts"]) > 1,
    )
    result = import_document(kb_dir, source)
    assert result.source_intake == "saved"
    assert result.knowledge_compilation == "completed", result
    rejected = [body for body, _, refused in calls if refused]
    assert rejected
    assert len({json.dumps(body["messages"], sort_keys=True) for body in rejected}) == len(rejected)
    assert all(body["max_tokens"] == 1024 for body, _, _ in calls)
    verified = [
        payload
        for _, payload, refused in calls
        if payload["stage"] == "verification" and not refused
    ]
    assert {fact["quote"] for payload in verified for fact in payload["facts"]} == set(paragraphs)
    assert all(len(payload["facts"]) == 1 for payload in verified)
    assert list((kb_dir / "wiki/concepts").glob("*.md"))
    assert result.usage["observable_attempts"] == len(calls)
    assert result.usage["unknown_usage"] == len(rejected)
    assert len(model_service) == len(calls) - len(rejected)

    before = len(calls)
    source.unlink()
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.source_intake == "saved"
    assert resumed.knowledge_compilation == "completed", resumed
    assert len(calls) == before


def test_unsplittable_provider_refusal_omits_only_affected_and_dependent_candidates(
    kb_dir, tmp_path, model_service, monkeypatch
):
    source = tmp_path / "capacity-dependencies.md"
    source.write_text(
        "# Alpha\n\nAlpha condition.\n\n"
        "# Beta\n\nBeta operation requires Alpha.\n\n"
        "# Gamma\n\nGamma listens on port 9342."
    )
    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["processing"].update(concurrency=1, max_requests=40, max_tokens=1000000)
    config_path.write_text(yaml.safe_dump(settings))
    dependency_requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, row in zip(payload["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
                else:
                    for fact in row["facts"]:
                        fact["topic"] = unit["headings"][-1]
        elif payload["stage"] == "planning":
            value = {
                "topics": [
                    {
                        "name": title.lower(),
                        "title": title,
                        "kind": "concept",
                        "members": [identity],
                    }
                    for identity, title in payload["topic_labels"].items()
                ]
            }
        elif payload["stage"] == "dependencies":
            dependency_requests.append(payload)
            value = {
                "topics": [
                    {
                        "path": row["path"],
                        "status": "dependent" if row["path"] == "concepts/beta" else "independent",
                        "reason": "Beta requires the missing Alpha condition; Gamma is unrelated.",
                    }
                    for row in payload["candidates"]
                ]
            }
        return value

    model_service.respond = respond
    calls = _refuse_context_requests(
        monkeypatch,
        lambda payload: payload["stage"] == "verification" and payload["title"] == "Alpha",
    )
    result = import_document(kb_dir, source)
    assert result.source_intake == "saved"
    assert result.knowledge_compilation == "completed", result
    rejected = [body for body, _, refused in calls if refused]
    assert len(rejected) == 1
    assert rejected[0]["max_tokens"] == 1024
    assert any(
        row["stage"] == "generation"
        and row["reason"] == "provider_context_exceeded"
        and row["items"] == ["concepts/alpha"]
        for row in result.omissions
    )
    assert dependency_requests
    assert any(
        "concepts/alpha" in row.get("items", [])
        for request in dependency_requests
        for row in request["omissions"]
    )
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert (kb_dir / "wiki/concepts/gamma.md").exists()
    assert result.usage["observable_attempts"] == len(calls)
    assert result.usage["unknown_usage"] == 1
