"""Provider capacity refusals preserve source intake and resumable compilation."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import litellm
import pytest
import yaml

from openkb.agent.document_windowing import bounded_windows
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


def test_shared_context_windowing_expands_before_splitting_target_evidence():
    """An explicit shared ceiling is exhausted before a W/T semantic split."""

    limits = RequestLimits.from_config(
        {
            "processing": {
                **DEFAULT_CONFIG["processing"],
                "context_tokens": 1000,
                "max_context_tokens": 10000,
                "output_tokens": 100,
                "max_output_tokens": 100,
            }
        }
    )
    source = SimpleNamespace(source_id="a" * 32, id="b" * 32)
    parsed = SimpleNamespace(
        id="c" * 32,
        blocks=[
            SimpleNamespace(
                id="d" * 32,
                order=0,
                kind="paragraph",
                location={},
                assets=[],
                context={},
                chars=5000,
            )
        ],
    )
    windows = [{"target_start": 0, "target_end": 1, "status": "complete"}]

    bounded, effective_limits = bounded_windows(source, parsed, windows, limits)

    assert bounded == windows
    assert effective_limits.context_tokens > limits.context_tokens
    assert effective_limits.input_capacity <= effective_limits.max_context_tokens - 100


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


def test_provider_refusal_omits_final_candidate_and_continue_reuses_generation(
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
        lambda payload: payload["stage"] == "verification"
        and len(payload["evidence"]["blocks"]) > 1,
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
    # The final candidate must be verified as a whole.  A provider refusal
    # therefore omits it instead of proving separate fragments and publishing
    # a result that was never reviewed in its assembled form.
    assert not verified
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert any(row["reason"] == "provider_context_exceeded" for row in result.omissions)
    assert result.usage["observable_attempts"] == len(calls)
    assert result.usage["unknown_usage"] == len(rejected)
    assert len(model_service) == len(calls) - len(rejected)

    before = len(calls)
    source.unlink()
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.source_intake == "saved"
    assert resumed.knowledge_compilation == "completed", resumed
    # The unsupported final candidate remains retryable, but Continue must not
    # regenerate it or substitute fragment-level verification.
    assert len(calls) == before + 1
    assert calls[-1][1]["stage"] == "verification"
    assert calls[-1][2]
    assert len(model_service) == before - len(rejected)


def test_unsplittable_provider_refusal_omits_only_affected_planned_page(
    kb_dir, tmp_path, model_service, monkeypatch
):
    source = tmp_path / "capacity-dependencies.md"
    source.write_text(
        "Alpha condition.\n\nBeta operation requires Alpha.\n\nGamma listens on port 9342."
    )
    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["processing"].update(concurrency=1, max_requests=40, max_tokens=1000000)
    config_path.write_text(yaml.safe_dump(settings))

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            value = {
                "overview": {
                    "text": "Three independent operating conditions.",
                    "ranges": [[0, 3]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": title.lower(),
                        "target_key": "",
                        "target": "",
                        "title": title,
                        "kind": "concept",
                        "name": f"concepts/{title.lower()}",
                        "purpose": f"{title} operating condition.",
                        "subject_ranges": [[start, start + 1]],
                        "necessary_context": [],
                    }
                    for start, title in enumerate(("Alpha", "Beta", "Gamma"))
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif payload["stage"] == "generation":
            value = {
                "content": "\n".join(block["text"] for block in payload["evidence"]["blocks"]),
                "covered": [row["id"] for row in payload["occurrences"]],
            }
        elif payload["stage"] == "verification":
            value = {"verdict": "supported", "reason": "Faithful original evidence.", "issues": []}
        return value

    model_service.respond = respond
    calls = _refuse_context_requests(
        monkeypatch,
        lambda payload: payload["stage"] == "verification" and payload["page"]["title"] == "Alpha",
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
    ), result.omissions
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert (kb_dir / "wiki/concepts/beta.md").exists()
    assert (kb_dir / "wiki/concepts/gamma.md").exists()
    assert result.usage["observable_attempts"] == len(calls)
    assert result.usage["unknown_usage"] == 1
