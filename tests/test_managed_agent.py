"""Agents SDK retries and tools share the task policy at the HTTP boundary."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from test_llm_execution import executor_for

pytest_plugins = ("test_vendor_sdk",)


def agent_for(executor, tools=()):
    from ultrakb.agent.managed_model import ManagedAgentModel

    model = ManagedAgentModel(executor)
    return Agent(
        name="fixture",
        model=model,
        tools=list(tools),
        model_settings=ModelSettings(retry=model.retry_settings(), include_usage=True),
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_reasoning_tool_roundtrip_uses_provider_protocol_without_visible_reasoning(
    model_service, stream
):
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    @function_tool
    def read_original() -> str:
        """Read the retained original measurement."""
        return "The original states 35 under load."

    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            model_service.credentials,
            generation={"answer": {"max_tokens": 1024, "reasoning_effort": "low"}},
        ),
        ModelCallPolicy(max_calls=4, retries=0),
    )
    agent = agent_for(executor, [read_original])
    agent.instructions = "Read the original exactly once and report its measurement with condition."
    agent.model_settings = ModelSettings(
        retry=agent.model.retry_settings(),
        include_usage=True,
    )
    if stream:
        from ultrakb.agent.query import iter_agent_response_events

        events = [
            event
            async for event in iter_agent_response_events(
                agent, "Read the source", run_config=RunConfig(tracing_disabled=True)
            )
        ]
        answer = events[-1]["data"]["answer"]
        public = json.dumps([event for event in events if event["event"] != "final"])
    else:
        run = await Runner.run(
            agent, "Read the source", run_config=RunConfig(tracing_disabled=True), max_turns=4
        )
        answer, public = run.final_output, run.final_output
    assert "35" in answer and "load" in answer.lower()
    assert 2 <= len(model_service.requests) <= 4
    second = model_service.requests[1]
    assert second["thinking"] == {"type": "enabled"}
    assistant = next(message for message in second["messages"] if message.get("tool_calls"))
    assert assistant.get("reasoning_content")
    assert assistant["reasoning_content"] not in public


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,delivered", [("openai/gpt-4o-mini", True), ("deepseek/deepseek-flash", False)]
)
async def test_all_main_model_providers_reject_images(model_service, model, delivered):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings

    image = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
        "QVR42mP8/x8AAwMCAO+jBv8AAAAASUVORK5CYII="
    )
    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle(api_key="fixture", base_url=model_service.url))
    )
    with pytest.raises(ValueError, match="text-only"):
        await Runner.run(
            agent_for(executor),
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Read the image."},
                        {"type": "input_image", "image_url": image},
                    ],
                }
            ],
            run_config=RunConfig(tracing_disabled=True),
        )
    assert not model_service.requests
    assert executor.calls == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_agent_retry_first_send_is_single_and_accounted(
    model_service, kb_dir, stream, monkeypatch
):
    import httpx
    from native_usage_fixtures import aggregate_usage, native_usage_execution, read_requests

    original = httpx.AsyncClient._send_single_request
    failures = []

    async def disconnect_once(client, outgoing):
        if outgoing.url.host == "127.0.0.1" and not failures:
            failures.append(True)
            raise httpx.ConnectError(
                "Local transport interrupted before connection", request=outgoing
            )
        return await original(client, outgoing)

    monkeypatch.setattr(httpx.AsyncClient, "_send_single_request", disconnect_once)
    with native_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service, retries=1, backoff=0)
        agent = agent_for(executor)
        agent.model_settings.max_tokens = 64
        if stream:
            run = Runner.run_streamed(
                agent, "Reply exactly ok in lowercase.", run_config=RunConfig(tracing_disabled=True)
            )
            async for _ in run.stream_events():
                pass
        else:
            run = await Runner.run(
                agent, "Reply exactly ok in lowercase.", run_config=RunConfig(tracing_disabled=True)
            )
    assert run.final_output.strip().lower() == "ok"
    records = read_requests(kb_dir)
    assert executor.calls == len(records) == 2 and len(model_service.requests) == 1
    assert len({record.logical_call_id for record in records}) == 1
    assert aggregate_usage(kb_dir)["input_total"] == run.context_wrapper.usage.input_tokens


@pytest.mark.asyncio
async def test_agent_tool_is_not_replayed_after_next_request_fails(model_service, monkeypatch):
    import httpx

    calls = []

    @function_tool
    def write_once(value: str) -> str:
        """Record one value."""
        calls.append(value)
        return "saved"

    original = httpx.AsyncClient._send_single_request
    failures = []

    async def disconnect_after_tool(client, outgoing):
        if outgoing.url.host == "127.0.0.1" and model_service.requests and not failures:
            failures.append(True)
            raise httpx.ConnectError(
                "Local delivery failed on the next model turn", request=outgoing
            )
        return await original(client, outgoing)

    monkeypatch.setattr(httpx.AsyncClient, "_send_single_request", disconnect_after_tool)
    executor = executor_for(model_service, retries=1, backoff=0)
    agent = agent_for(executor, [write_once])
    agent.model_settings.max_tokens = 128
    run = await Runner.run(
        agent,
        'Call write_once exactly once with value "one", then reply exactly ok.',
        run_config=RunConfig(tracing_disabled=True),
        max_turns=4,
    )
    assert run.final_output.strip().lower() == "ok" and calls == ["one"]
    assert len(model_service.requests) == 2 and executor.calls == 3


@pytest.mark.asyncio
async def test_stream_without_usage_remains_unknown(model_service, kb_dir):
    from ultrakb.agent.managed_model import _ReceiptModel
    from ultrakb.llm_usage import normalize_usage

    inner = _ReceiptModel(executor_for(model_service), None)
    try:
        assert inner.raw_usage is None
        assert inner.public_usage()["input_tokens"] is None
        assert inner.public_usage()["output_tokens"] is None
        assert normalize_usage(None)["usage_status"] == "unknown"
    finally:
        await inner.close()
    assert not model_service.requests


@pytest.mark.asyncio
async def test_agent_cancel_waits_for_cleanup_without_retry(model_service, kb_dir):
    from native_usage_fixtures import native_usage_execution, read_requests

    from ultrakb.locks import LockCancelled

    warm_agent = agent_for(executor_for(model_service))
    warm_agent.model_settings.max_tokens = 64
    warm = Runner.run_streamed(
        warm_agent, "Reply exactly ok.", run_config=RunConfig(tracing_disabled=True)
    )
    async for _ in warm.stream_events():
        pass
    model_service.requests.clear()
    model_service.delay = 0.3
    stopped = threading.Event()
    with native_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service, cancelled=stopped.is_set)
        agent = agent_for(executor)
        agent.model_settings.max_tokens = 64
        run = Runner.run_streamed(
            agent, "Reply exactly ok.", run_config=RunConfig(tracing_disabled=True)
        )

        async def consume():
            async for _ in run.stream_events():
                pass

        task = asyncio.create_task(consume())
        for _ in range(200):
            if model_service.requests:
                break
            await asyncio.sleep(0.01)
        assert model_service.requests
        stopped.set()
        with pytest.raises(LockCancelled):
            await task
    assert len(model_service.requests) == 1
    assert read_requests(kb_dir)[0].state == "cancelled"


@pytest.mark.asyncio
async def test_failed_stream_after_text_is_never_replayed(model_service, live_llm, monkeypatch):
    delivered = []

    async def interrupt_delivery(**kwargs):
        source = await live_llm.acompletion(**kwargs)

        async def chunks():
            try:
                async for chunk in source:
                    yield chunk  # Forward the genuine provider frame unchanged.
                    if chunk.choices and chunk.choices[0].delta.content:
                        delivered.append(chunk.choices[0].delta.content)
                        raise ConnectionError("Local stream delivery interrupted after real text")
            finally:
                await source.aclose()

        return chunks()

    monkeypatch.setattr("litellm.acompletion", interrupt_delivery)
    executor = executor_for(model_service, retries=2, backoff=0)
    agent = agent_for(executor)
    agent.model_settings.max_tokens = 64
    run = Runner.run_streamed(
        agent, "Reply exactly ok.", run_config=RunConfig(tracing_disabled=True)
    )
    deltas = []
    with pytest.raises(ConnectionError):
        async for event in run.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                deltas.append(event.data.delta)
    assert delivered and "".join(deltas) == "".join(delivered)
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_two_agent_tasks_keep_bindings_and_ledgers_separate(model_service, kb_dir, tmp_path):
    from dataclasses import replace

    from native_usage_fixtures import native_usage_execution, read_requests

    from ultrakb.llm_execution import CompletionExecutor, RoleBindings

    other_root = tmp_path / "other"
    other_root.mkdir()
    fingerprints = []

    async def run(root, task):
        with native_usage_execution(root, "answer"):
            executor = CompletionExecutor(
                RoleBindings(
                    "deepseek/deepseek-flash",
                    replace(model_service.credentials, extra_headers={"x-test-task": task}),
                    generation={"answer": {"max_tokens": 64 if task == "a" else 128}},
                )
            )
            fingerprints.append(executor.fingerprint)
            agent = agent_for(executor)
            agent.model_settings.max_tokens = 64
            result = await Runner.run(
                agent, "Reply exactly ok.", run_config=RunConfig(tracing_disabled=True)
            )
            assert result.final_output.strip().lower() == "ok"

    await asyncio.gather(run(kb_dir, "a"), run(other_root, "b"))
    tasks = [
        {k.lower(): v for k, v in headers.items()}["x-test-task"]
        for _, headers, _ in model_service.records
    ]
    assert set(tasks) == {"a", "b"} and len(fingerprints) == len(set(fingerprints)) == 2
    assert [record.model for record in read_requests(kb_dir)] == ["deepseek/deepseek-flash"]
    assert [record.model for record in read_requests(other_root)] == ["deepseek/deepseek-flash"]


@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
@pytest.mark.asyncio
async def test_real_application_commits_only_complete_answers(
    model_service, kb_dir, chat, outcome, monkeypatch, live_llm
):
    from native_usage_fixtures import read_requests

    from ultrakb.application.conversations import (
        ask_question,
        continue_conversation,
        read_conversation,
    )
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  max_calls: 4\n  retries: 0\n"
        "role_generation:\n  answer:\n    max_tokens: 128\n    reasoning_effort: none\n",
    )
    atomic_write_text(kb_dir / "wiki/index.md", "# Knowledge\nOrion costs 17 credits.")
    atomic_write_text(kb_dir / "wiki/sources/stream-fixture.md", "Orion costs 17 credits.")
    if outcome == "error":

        async def interrupted(**kwargs):
            original = await live_llm.acompletion(**kwargs)

            async def chunks():
                try:
                    async for chunk in original:
                        yield chunk
                        if chunk.choices and chunk.choices[0].delta.content:
                            raise ConnectionError("Local stream delivery interrupted")
                finally:
                    await original.aclose()

            return chunks()

        monkeypatch.setattr("litellm.acompletion", interrupted)
    stopped = threading.Event()

    def receive(event):
        if outcome == "cancel" and event.get("event") == "delta":
            stopped.set()

    context = ExecutionContext(cancelled=stopped.is_set, on_event=receive)
    question = (
        "Read index.md, then read_file sources/stream-fixture.md. "
        "Give only the current Orion cost in credits from that original."
    )
    if chat:
        result = await continue_conversation(kb_dir, question, context=context)
        session = read_conversation(kb_dir, result.session_id)
        assert session.total_turns == (1 if outcome == "complete" else 0)
    else:
        result = await ask_question(kb_dir, question, save=True, context=context)
        assert bool(result.saved_path) == (outcome == "complete")
    assert (result.status == "completed") == (outcome == "complete")
    if outcome == "complete":
        assert "17" in result.answer
        originals = [
            message
            for request in model_service.requests
            for message in request["messages"]
            if message["role"] == "tool"
        ]
        assert "Orion costs 17 credits." in json.dumps(originals)
        if chat:
            assert session.turns[0][1] == result.answer
        else:
            assert result.answer in Path(result.saved_path).read_text()
    assert len(model_service.requests) == len(read_requests(kb_dir))
    assert 1 <= len(model_service.requests) <= 4
