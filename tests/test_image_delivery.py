"""Image coverage reflects the provider payload, not just the SDK tool result."""

import asyncio
import json
import threading

import pytest
from agents import ModelSettings, RunConfig, Runner
from PIL import Image
from test_answer_evidence_regression import agent_for, invoke
from test_original_context import pages
from test_vendor_sdk import response, streamed_response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "model,expected",
    [("openai/gpt-4o-mini", "pixels_delivered"), ("deepseek/deepseek-flash", "failed")],
)
async def test_image_delivery_is_confirmed_at_the_provider_boundary(
    kb_dir, model_service, model, expected, stream
):
    from openkb.agent.managed_model import ManagedAgentModel
    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, RoleBindings
    from openkb.llm_images import request_image_digests

    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# First\nFigure", "# Second\nFigure"], ["sources/figure.png"])
    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle(api_key="fixture", base_url=model_service.url))
    )
    with executor.activate():
        agent = agent_for(kb_dir)
    agent.model = ManagedAgentModel(executor)
    agent.model_settings = ModelSettings(retry=agent.model.retry_settings(), include_usage=True)
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1-2")
    occurrences = json.loads(content.split("\nImage coverage: ")[1])["occurrences"]
    call = {
        "id": "figure-read",
        "type": "function",
        "function": {
            "name": "get_image",
            "arguments": json.dumps({"image_path": occurrences[0]["occurrence_id"]}),
        },
    }
    make_response = streamed_response if stream else response
    model_service.replies.extend(
        [
            (
                200,
                make_response(
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    reason="tool_calls",
                ),
            ),
            (200, make_response({"role": "assistant", "content": "Read attempted."})),
        ]
    )
    if stream:
        run = Runner.run_streamed(
            agent, "Read the first figure.", run_config=RunConfig(tracing_disabled=True)
        )
        async for _ in run.stream_events():
            pass
    else:
        await Runner.run(
            agent, "Read the first figure.", run_config=RunConfig(tracing_disabled=True)
        )
    sent = model_service.requests[-1]["messages"]
    assert bool(request_image_digests(sent)) is (expected == "pixels_delivered")
    tool_result = next(message["content"] for message in sent if message["role"] == "tool")
    text = tool_result if isinstance(tool_result, str) else tool_result[0]["text"]
    assert json.loads(text)["status"] == "delivery_pending"
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1-2")
    images = json.loads(content.split("\nImage coverage: ")[1])["occurrences"]
    assert [image["status"] for image in images] == [expected, "unread"]
    if expected == "failed":
        assert images[0]["reason_code"] == "image_not_sent"
    else:
        for _ in range(5):
            await invoke(agent, "get_image", image_path=images[0]["occurrence_id"])
        failure = json.loads(
            await invoke(agent, "get_image", image_path=images[0]["occurrence_id"])
        )
        assert failure["reason_code"] == "image_capacity"
        content = await invoke(agent, "get_page_content", doc_name="manual", pages="1-2")
        images = json.loads(content.split("\nImage coverage: ")[1])["occurrences"]
        assert [image["status"] for image in images] == ["pixels_delivered", "unread"]
        assert executor.image_deliveries.snapshot()[-1]["reason_code"] == "image_capacity"


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls", [1, 2])
async def test_question_returns_image_receipts_even_without_a_following_send(
    kb_dir, model_service, monkeypatch, max_calls
):
    from agents import set_tracing_disabled

    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext
    from openkb.config import load_config, save_config

    set_tracing_disabled(True)
    monkeypatch.setenv("LLM_API_KEY", "fixture")
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    config_path = kb_dir / ".openkb/config.yaml"
    config = load_config(config_path)
    config.update(model="deepseek/deepseek-flash", model_policy={"max_calls": max_calls})
    save_config(config_path, config)
    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# Figure\nImage"], ["sources/figure.png"])
    tool = {
        "id": "figure",
        "type": "function",
        "function": {"name": "get_image", "arguments": '{"image_path":"sources/figure.png"}'},
    }
    model_service.replies.append(
        (
            200,
            streamed_response(
                {"role": "assistant", "content": None, "tool_calls": [tool]}, reason="tool_calls"
            ),
        )
    )
    if max_calls > 1:
        model_service.replies.append(
            (200, streamed_response({"role": "assistant", "content": "No pixels received."}))
        )
    events = []
    result = await ask_question(
        kb_dir, "Read the figure", context=ExecutionContext(on_event=events.append)
    )
    assert result.status == ("completed" if max_calls == 2 else "failed")
    assert len(model_service.requests) == max_calls
    if max_calls == 1:
        assert not model_service.requests[0].get("tools")
        assert not result.image_coverage  # Tool-disabled finish rejects the rogue call.
        return
    assert result.image_coverage[0]["status"] == "failed"
    assert result.image_coverage[0]["reason_code"] == "image_not_sent"
    assert [
        event["data"]["images"] for event in events if event.get("event") == "image_coverage"
    ] == [list(result.image_coverage)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["retry", "reject", "cancel", "length_without_usage", "unobserved", "credentials"]
)
async def test_image_delivery_keeps_request_failures_and_unknowns(
    kb_dir, model_service, monkeypatch, ending
):
    from agents.items import ItemHelpers
    from openai.types.responses import ResponseFunctionToolCall

    from openkb.agent.managed_model import ManagedAgentModel
    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# Figure\nImage"], ["sources/figure.png"])
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(
                api_key="" if ending == "credentials" else "fixture",
                base_url=model_service.url,
            ),
        ),
        ModelCallPolicy(retries=1 if ending == "retry" else 0, backoff=0),
    )
    with executor.activate():
        agent = agent_for(kb_dir)
    agent.model = ManagedAgentModel(executor)
    agent.model_settings = ModelSettings(retry=agent.model.retry_settings())
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    image = json.loads(content.split("\nImage coverage: ")[1])["occurrences"][0]
    output = await invoke(agent, "get_image", image_path=image["occurrence_id"])
    call = ResponseFunctionToolCall(
        name="get_image", arguments="{}", call_id="figure", type="function_call"
    )
    history = [
        {"role": "user", "content": "Read the figure."},
        call.model_dump(),
        ItemHelpers.tool_call_output_item(call, output),
    ]
    if ending in {"retry", "reject"}:
        model_service.replies.append((503, {"error": {"message": "Unavailable"}}))
    if ending in {"retry", "length_without_usage"}:
        model_service.replies.append((200, response(reason="length", usage=False)))
    started = threading.Event()
    if ending == "credentials":
        monkeypatch.setenv("OPENAI_API_KEY", "uncaptured-fixture")
    if ending == "unobserved":
        from litellm import ModelResponse

        async def provider_without_httpx(**kwargs):
            return ModelResponse(**response())

        monkeypatch.setattr("litellm.acompletion", provider_without_httpx)
    if ending == "cancel":

        def reply(request):
            started.set()
            return response()

        model_service.replies.append((200, reply))
        model_service.delay = 0.8
    task = asyncio.create_task(
        Runner.run(agent, history, run_config=RunConfig(tracing_disabled=True))
    )
    if ending == "cancel":
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif ending == "credentials":
        from litellm.exceptions import AuthenticationError

        with pytest.raises(AuthenticationError):
            await task
    elif ending == "reject":
        from litellm.exceptions import ServiceUnavailableError

        with pytest.raises(ServiceUnavailableError):
            await task
    else:
        await task
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    image = json.loads(content.split("\nImage coverage: ")[1])["occurrences"][0]
    expected = {
        "reject": "failed",
        "cancel": "delivery_unknown",
        "unobserved": "delivery_unknown",
        "credentials": "failed",
    }.get(ending, "pixels_delivered")
    assert image["status"] == expected
    assert len(image["attempts"]) == (2 if ending == "retry" else 1)
    identities = [identity for attempt in image["attempts"] for identity in attempt["request_ids"]]
    assert len(identities) == len(set(identities)) == len(model_service.requests)
    if ending == "retry":
        assert [attempt["status"] for attempt in image["attempts"]] == [
            "failed",
            "pixels_delivered",
        ]
        assert "reason_code" not in image
