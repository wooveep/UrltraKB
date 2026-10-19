"""Soft stops finish inside the existing SDK loop; hard stops never publish narration."""

import json
from pathlib import Path

import pytest

pytest_plugins = ("test_vendor_sdk",)


def configure(kb_dir, service, calls):
    from agents import set_tracing_disabled

    from ultrakb.locks import atomic_write_text

    set_tracing_disabled(True)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nmodel_policy:\n"
        f"  max_calls: {calls}\n  retries: 0\nrole_generation:\n  answer:\n"
        "    max_tokens: 256\n    reasoning_effort: none\n",
    )
    atomic_write_text(
        kb_dir / "wiki/index.md", "# Index\n\n[sources/evidence.md](sources/evidence.md)"
    )
    atomic_write_text(
        kb_dir / "wiki/sources/evidence.md", "Only under winter conditions, retain seven days."
    )


@pytest.mark.asyncio
async def test_last_send_is_reserved_for_a_tool_disabled_final_answer(kb_dir, model_service):
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext

    # The production prompt reads the index before the original evidence.
    configure(kb_dir, model_service, 3)
    events = []
    context = ExecutionContext(on_event=events.append)
    result = await ask_question(
        kb_dir,
        "Read index.md, then sources/evidence.md, then compare winter and summer retention.",
        save=True,
        context=context,
    )
    assert result.answer_outcome == "partial" and result.unfinished, result.error
    assert result.saved_path and "seven days" in result.answer
    assert len(model_service.requests) == context.executor.calls == 3
    final = model_service.requests[-1]
    assert not final.get("tools")
    assert "Only under winter conditions" in json.dumps(final["messages"])
    assert "DSML" not in result.answer and "<invoke" not in result.answer
    assert "DSML" not in Path(result.saved_path).read_text()
    assert any(e.get("event") == "activity" and e["data"].get("stage") == "reading" for e in events)


def test_only_one_send_cannot_start_a_tool_loop_or_publish_its_narration(kb_dir, model_service):
    from agents import Agent
    from openai.types.responses import ResponseFunctionToolCall
    from test_llm_execution import executor_for

    from ultrakb.agent.answer_finalization import AnswerFinisher
    from ultrakb.llm_execution import ModelBudgetExceeded

    executor = executor_for(model_service, max_calls=1)
    original_tools = [object()]
    agent = Agent(name="admission", tools=original_tools)
    finisher = AnswerFinisher(executor, agent, max_turns=10)
    finisher.prepare()
    assert agent.tools == [] and finisher.reason == "call_budget"
    # Validate delivered protocol data directly; no synthetic model or Runner response.
    request = ResponseFunctionToolCall(
        type="function_call",
        call_id="read",
        name="read_file",
        arguments='{"path":"sources/evidence.md"}',
    )
    with pytest.raises(ModelBudgetExceeded, match="disabled-tool"):
        finisher.receive([request])
    assert not finisher.returned and finisher.unfinished
    finisher.close()
    assert agent.tools is original_tools
    assert model_service.requests == []
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))


def test_sdk_retry_reserve_signal_switches_to_finalization(model_service):
    from types import SimpleNamespace

    from agents import Agent
    from test_llm_execution import executor_for

    from ultrakb.agent.answer_finalization import AnswerFinisher
    from ultrakb.agent.managed_model import ManagedAgentModel
    from ultrakb.llm_execution import AnswerReserveReached

    executor = executor_for(model_service, max_calls=3, retries=1)
    original_tools = [object()]
    agent = Agent(name="reserve", tools=original_tools)
    model = ManagedAgentModel(executor)
    model.finisher = AnswerFinisher(executor, agent, max_turns=10)
    executor.reserve("answer")
    executor.reserve("answer")
    context = SimpleNamespace(error=AnswerReserveReached("Final send is protected"))
    assert model.retry_settings().policy(context)
    assert model.finisher.reason == "call_budget" and agent.tools == []
    assert executor.budget.snapshot()["reserved"] == {}
    executor.reserve("answer")
    assert executor.calls == 3
    model.finisher.receive([])
    assert not model.retry_settings().policy(context)
    model.finisher.close()
    assert agent.tools is original_tools and model_service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hard", [False, True])
async def test_deadline_after_read_has_separate_soft_and_hard_exits(
    kb_dir, model_service, monkeypatch, hard
):
    import time
    from types import SimpleNamespace

    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 10)
    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text(
        config.read_text().replace("  retries: 0", "  retries: 0\n  deadline_seconds: 30")
    )
    events = []

    def now():
        read = any(
            e.get("event") == "activity"
            and e["data"].get("stage") == "reading"
            and e["data"].get("status") == "completed"
            for e in events
        )
        return time.monotonic() + ((40 if hard else 20) if read else 0)

    clock = SimpleNamespace(monotonic=now, sleep=time.sleep)
    monkeypatch.setattr("ultrakb.llm_execution.time", clock)
    monkeypatch.setattr("ultrakb.execution_budget.time", clock)
    result = await ask_question(
        kb_dir,
        "Call read_file for sources/evidence.md first, then state winter retention.",
        save=True,
        context=ExecutionContext(on_event=events.append),
    )
    assert result.unfinished
    if hard:
        assert result.status == "failed" and not result.saved_path
        assert len(model_service.requests) == 1
    else:
        assert result.status == "completed" and result.answer_outcome == "partial"
        assert result.saved_path and "deadline" in str(result.unfinished)
        assert len(model_service.requests) == 2 and not model_service.requests[-1].get("tools")


@pytest.mark.asyncio
async def test_capacity_hard_limit_does_not_send_or_save(kb_dir, model_service):
    from ultrakb.application.conversations import ask_question

    configure(kb_dir, model_service, 10)
    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text(
        config.read_text().replace("max_input_tokens: 65536", "max_input_tokens: 600")
    )
    result = await ask_question(
        kb_dir,
        "Call read_file for sources/evidence.md first, then state winter retention.",
        save=True,
    )
    assert result.status == "blocked" and result.unfinished and not result.saved_path
    assert len(model_service.requests) == 0


@pytest.mark.asyncio
async def test_sdk_retry_cannot_consume_the_final_answer_reserve(
    kb_dir, model_service, monkeypatch
):
    import httpx

    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 3)
    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text(config.read_text().replace("retries: 0", "retries: 1\n  backoff: 0"))
    original = httpx.AsyncClient._send_single_request
    interruptions = []

    async def disconnect_once(client, outgoing):
        if outgoing.url.host == "127.0.0.1" and model_service.requests and not interruptions:
            interruptions.append(True)
            raise httpx.ConnectError(
                "Local connection interrupted before provider request", request=outgoing
            )
        return await original(client, outgoing)

    monkeypatch.setattr(httpx.AsyncClient, "_send_single_request", disconnect_once)
    context = ExecutionContext()
    result = await ask_question(
        kb_dir,
        "Call read_file for sources/evidence.md, then state the retention conditions.",
        save=True,
        context=context,
    )
    assert result.status == "completed" and result.answer_outcome == "partial", result.error
    assert interruptions and context.executor.calls == 3
    assert len(model_service.requests) == 2
    assert model_service.requests[0].get("tools")
    assert not model_service.requests[-1].get("tools")
    assert "Only under winter conditions" in json.dumps(model_service.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_round_limit_reserves_last_real_sdk_turn(kb_dir, model_service):
    from ultrakb.agent.query import build_query_agent, iter_agent_response_events
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    configure(kb_dir, model_service, 10)
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir) as bundle:
        agent = build_query_agent(str(kb_dir / "wiki"), "deepseek/deepseek-flash", bundle=bundle)
        events = [
            e
            async for e in iter_agent_response_events(
                agent,
                "Call read_file for sources/evidence.md first, then state winter retention.",
                max_turns=2,
            )
        ]
    final = next(e["data"] for e in events if e["event"] == "final")
    assert final["answer_outcome"] == "partial" and "turn_limit" in str(final["unfinished"])
    assert len(model_service.requests) == 2 and not model_service.requests[-1].get("tools")


@pytest.mark.asyncio
async def test_cancellation_after_original_read_never_saves_narration(kb_dir, model_service):
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 5)
    events = []

    def read_finished():
        return any(
            e.get("event") == "activity"
            and e["data"].get("stage") == "reading"
            and e["data"].get("status") == "completed"
            for e in events
        )

    context = ExecutionContext(on_event=events.append, cancelled=read_finished)
    result = await ask_question(
        kb_dir,
        "Call read_file for sources/evidence.md first, then state winter retention.",
        save=True,
        context=context,
    )
    assert read_finished(), events
    assert result.status == "stopped" and not result.saved_path and result.unfinished
    assert len(model_service.requests) == 1
    assert not any(e.get("event") == "final" for e in events)
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))


@pytest.mark.asyncio
async def test_long_conversation_preparation_preserves_the_only_answer_send(
    kb_dir, model_service, monkeypatch
):
    from conversation_fixtures import seed_conversation

    from ultrakb.application.conversations import continue_conversation
    from ultrakb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 1)
    monkeypatch.setattr("ultrakb.context.preparation.RECALL_THRESHOLD_TOKENS", 1)
    session = seed_conversation(
        kb_dir,
        [(f"Preference {index}: quiet indoor workspace.", "Display only.") for index in range(9)],
    )
    context = ExecutionContext()
    result = await continue_conversation(
        kb_dir, "Confirm my latest preference.", session_id=session.id, context=context
    )
    assert result.status == "completed", result.error
    assert result.answer_outcome == "partial" and result.unfinished
    assert len(model_service.requests) == context.executor.calls == 1
    assert not model_service.requests[0].get("tools")
    assert context.executor.budget.snapshot()["stages"] == {"answer": 1}
    assert context.executor.budget.snapshot()["reserved"] == {}
