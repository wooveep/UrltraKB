"""Agents SDK retries and tools share the task policy at the HTTP boundary."""

import asyncio
import threading

import pytest
from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from test_llm_execution import executor_for
from test_vendor_sdk import response, streamed_response

pytest_plugins = ("test_vendor_sdk",)


def agent_for(executor, tools=()):
    from openkb.agent.managed_model import ManagedAgentModel

    model = ManagedAgentModel(executor)
    return Agent(
        name="fixture",
        model=model,
        tools=list(tools),
        model_settings=ModelSettings(retry=model.retry_settings(), include_usage=True),
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_agent_retry_first_send_is_single_and_accounted(model_service, kb_dir, stream):
    from openkb.llm_usage import aggregate_usage, read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.extend(
        [
            (503, {"error": {"message": "busy"}}),
            (
                200,
                streamed_response({"role": "assistant", "content": "ok"}) if stream else response(),
            ),
        ]
    )
    with import_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service, retries=1, backoff=0)
        agent = agent_for(executor)
        if stream:
            run = Runner.run_streamed(agent, "hello", run_config=RunConfig(tracing_disabled=True))
            async for _ in run.stream_events():
                pass
        else:
            run = await Runner.run(agent, "hello", run_config=RunConfig(tracing_disabled=True))
    assert run.final_output == "ok"
    records = read_requests(kb_dir)
    assert len(model_service.requests) == executor.calls == len(records) == 2
    assert len({r.logical_call_id for r in records}) == 1
    assert aggregate_usage(kb_dir)["input_total"] == 12


@pytest.mark.asyncio
async def test_agent_tool_is_not_replayed_after_next_request_fails(model_service):
    calls = []

    @function_tool
    def write_once(value: str) -> str:
        """Record one value."""
        calls.append(value)
        return "saved"

    tool = {
        "id": "t1",
        "type": "function",
        "function": {"name": "write_once", "arguments": '{"value":"one"}'},
    }
    model_service.replies.extend(
        [
            (
                200,
                response(
                    {"role": "assistant", "content": None, "tool_calls": [tool]},
                    reason="tool_calls",
                ),
            ),
            (503, {"error": {"message": "busy"}}),
            (200, response()),
        ]
    )
    executor = executor_for(model_service, retries=1, backoff=0)
    run = await Runner.run(
        agent_for(executor, [write_once]), "write", run_config=RunConfig(tracing_disabled=True)
    )
    assert run.final_output == "ok" and calls == ["one"]
    assert len(model_service.requests) == 3


@pytest.mark.asyncio
async def test_stream_without_usage_remains_unknown(model_service, kb_dir):
    from openkb.agent.query import iter_agent_response_events
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    chunks = streamed_response({"role": "assistant", "content": "ok"})
    chunks = [c for c in chunks if not c.get("usage")]
    model_service.replies.append((200, chunks))
    with import_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service)
        events = [
            e
            async for e in iter_agent_response_events(
                agent_for(executor), "hi", run_config=RunConfig(tracing_disabled=True)
            )
        ]
    assert events[-1]["event"] == "final"
    assert events[-1]["data"]["usage"]["input_uncached"] is None
    assert read_requests(kb_dir)[0].input_total is None


@pytest.mark.asyncio
async def test_agent_cancel_waits_for_cleanup_without_retry(model_service, kb_dir):
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.locks import LockCancelled

    stopped = threading.Event()
    model_service.delay = 0.3
    model_service.replies.append((200, streamed_response({"role": "assistant", "content": "ok"})))
    with import_usage_execution(kb_dir, "answer"):
        executor = executor_for(model_service, cancelled=stopped.is_set)
        run = Runner.run_streamed(
            agent_for(executor), "hi", run_config=RunConfig(tracing_disabled=True)
        )

        async def consume():
            async for _ in run.stream_events():
                pass

        task = asyncio.create_task(consume())
        for _ in range(100):
            if model_service.requests:
                break
            await asyncio.sleep(0.01)
        stopped.set()
        with pytest.raises(LockCancelled):
            await task
    assert len(model_service.requests) == 1
    assert read_requests(kb_dir)[0].state == "cancelled"


@pytest.mark.asyncio
async def test_failed_stream_after_text_is_never_replayed(model_service):
    first = streamed_response({"role": "assistant", "content": "partial"})[0]
    model_service.replies.extend(
        [
            (200, [first, {"error": {"message": "interrupted", "code": 503}}]),
            (200, streamed_response({"role": "assistant", "content": "duplicate"})),
        ]
    )
    executor = executor_for(model_service, retries=2, backoff=0)
    run = Runner.run_streamed(
        agent_for(executor), "hi", run_config=RunConfig(tracing_disabled=True)
    )
    deltas = []
    with pytest.raises(Exception):
        async for event in run.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                deltas.append(event.data.delta)
    assert deltas == ["partial"]
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_two_agent_tasks_keep_bindings_and_ledgers_separate(
    model_service, kb_dir, tmp_path, monkeypatch
):
    from test_vendor_sdk import running_model_service

    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, RoleBindings
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    other_root = tmp_path / "other"
    other_root.mkdir()

    async def run(service, root, key, model):
        with import_usage_execution(root, "answer"):
            executor = CompletionExecutor(
                RoleBindings(model, LlmCredentialBundle(api_key=key, base_url=service.url))
            )
            result = await Runner.run(
                agent_for(executor), "hi", run_config=RunConfig(tracing_disabled=True)
            )
            assert result.final_output == "ok"

    with running_model_service(monkeypatch) as other:
        model_service.replies.append((200, response()))
        other.replies.append((200, response()))
        await asyncio.gather(
            run(model_service, kb_dir, "key-a", "openai/gpt-4o-mini"),
            run(other, other_root, "key-b", "openai/gpt-4o"),
        )
        assert {k.lower(): v for k, v in model_service.records[0][1].items()}[
            "authorization"
        ] == "Bearer key-a"
        assert {k.lower(): v for k, v in other.records[0][1].items()}[
            "authorization"
        ] == "Bearer key-b"
    assert [r.model for r in read_requests(kb_dir)] == ["openai/gpt-4o-mini"]
    assert [r.model for r in read_requests(other_root)] == ["openai/gpt-4o"]


@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
@pytest.mark.asyncio
async def test_real_application_commits_only_complete_answers(
    model_service, kb_dir, chat, outcome, monkeypatch
):
    from agents import set_tracing_disabled

    from openkb.agent.chat_session import load_session
    from openkb.application.conversations import ask_question, continue_conversation
    from openkb.application.execution import ExecutionContext
    from openkb.llm_usage import read_requests

    set_tracing_disabled(True)
    monkeypatch.setenv("LLM_API_KEY", "fixture")
    (kb_dir / ".openkb/config.yaml").write_text("model: openai/gpt-4o-mini\nlanguage: en\n")
    (kb_dir / "wiki/index.md").write_text("# Knowledge\nAvailable knowledge.")
    chunks = streamed_response({"role": "assistant", "content": "Saved answer."})
    if outcome == "error":
        chunks = [chunks[0], {"error": {"message": "broken", "code": 503}}]
    model_service.replies.append((200, chunks))
    stopped = threading.Event()

    def receive(event):
        if outcome == "cancel" and event.get("event") == "delta":
            stopped.set()

    context = ExecutionContext(cancelled=stopped.is_set, on_event=receive)
    if chat:
        result = await continue_conversation(kb_dir, "Explain knowledge", context=context)
        session = load_session(kb_dir, result.session_id)
        assert session.turn_count == (1 if outcome == "complete" else 0)
    else:
        result = await ask_question(kb_dir, "Explain knowledge", save=True, context=context)
        assert bool(result.saved_path) == (outcome == "complete")
    assert (result.status == "completed") == (outcome == "complete")
    assert len(model_service.requests) == len(read_requests(kb_dir)) == 1
