"""Soft stops finish inside the existing SDK loop; hard stops never publish narration."""

import json

import pytest
from test_vendor_sdk import streamed_response

pytest_plugins = ("test_vendor_sdk",)


def configure(kb_dir, service, calls):
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nmodel_policy:\n"
        f"  max_calls: {calls}\n  retries: 0\n"
    )
    (kb_dir / "wiki/index.md").write_text("# Index\n\n[sources/evidence.md](sources/evidence.md)")
    (kb_dir / "wiki/sources/evidence.md").write_text(
        "Only under winter conditions, retain seven days."
    )


def tool_request():
    return {
        "role": "assistant",
        "content": "I will read the required conditions.",
        "tool_calls": [
            {
                "id": "read-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "sources/evidence.md"}),
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_last_send_is_reserved_for_a_tool_disabled_final_answer(kb_dir, model_service):
    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 2)
    model_service.replies.extend(
        [
            (200, streamed_response(tool_request(), "tool_calls")),
            (
                200,
                streamed_response(
                    {
                        "role": "assistant",
                        "content": "Winter: retain seven days. Summer remains unconfirmed.",
                    }
                ),
            ),
        ]
    )
    events = []
    context = ExecutionContext(on_event=events.append)
    result = await ask_question(
        kb_dir, "Read the original and compare winter and summer.", save=True, context=context
    )
    assert result.answer_outcome == "partial" and result.unfinished, result.error
    assert result.saved_path and "seven days" in result.answer
    assert len(model_service.requests) == context.executor.calls == 2
    final = model_service.requests[-1]
    assert not final.get("tools")
    assert "Only under winter conditions" in json.dumps(final["messages"])
    assert any(e.get("event") == "activity" and e["data"].get("stage") == "reading" for e in events)


@pytest.mark.asyncio
async def test_only_one_send_cannot_start_a_tool_loop_or_publish_its_narration(
    kb_dir, model_service
):
    from openkb.application.conversations import ask_question

    configure(kb_dir, model_service, 1)
    model_service.replies.append((200, streamed_response(tool_request(), "tool_calls")))
    result = await ask_question(kb_dir, "Read the original conditions.", save=True)
    assert result.status != "completed" and result.answer_outcome != "answered"
    assert not result.saved_path and result.unfinished
    assert len(model_service.requests) == 1, result.error
    assert not model_service.requests[0].get("tools")
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("hard", [False, True])
async def test_deadline_after_read_has_separate_soft_and_hard_exits(
    kb_dir, model_service, monkeypatch, hard
):
    import time
    from types import SimpleNamespace

    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 10)
    config = kb_dir / ".openkb/config.yaml"
    config.write_text(config.read_text() + "  deadline_seconds: 30\n")
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
    monkeypatch.setattr("openkb.llm_execution.time", clock)
    monkeypatch.setattr("openkb.execution_budget.time", clock)
    model_service.replies.extend(
        [
            (200, streamed_response(tool_request(), "tool_calls")),
            (200, streamed_response({"role": "assistant", "content": "Winter: seven days."})),
        ]
    )
    result = await ask_question(
        kb_dir, "Read the original.", save=True, context=ExecutionContext(on_event=events.append)
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
    from openkb.application.conversations import ask_question

    configure(kb_dir, model_service, 10)
    config = kb_dir / ".openkb/config.yaml"
    config.write_text(
        config.read_text().replace("max_input_tokens: 65536", "max_input_tokens: 600")
    )
    result = await ask_question(kb_dir, "Read the original.", save=True)
    assert result.status == "blocked" and result.unfinished and not result.saved_path
    assert len(model_service.requests) == 0


@pytest.mark.asyncio
async def test_sdk_retry_cannot_consume_the_final_answer_reserve(kb_dir, model_service):
    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 3)
    config = kb_dir / ".openkb/config.yaml"
    config.write_text(config.read_text().replace("retries: 0", "retries: 1\n  backoff: 0"))
    model_service.replies.extend(
        [
            (200, streamed_response(tool_request(), "tool_calls")),
            (503, {"error": {"message": "Temporary provider failure"}}),
            (200, streamed_response({"role": "assistant", "content": "Winter: seven days."})),
        ]
    )
    context = ExecutionContext()
    result = await ask_question(kb_dir, "Read the conditions.", save=True, context=context)
    assert result.status == "completed" and result.answer_outcome == "partial", result.error
    assert len(model_service.requests) == context.executor.calls == 3
    assert model_service.requests[1].get("tools")
    assert not model_service.requests[2].get("tools")
    assert "Only under winter conditions" in json.dumps(model_service.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_round_limit_reserves_last_real_sdk_turn(kb_dir, model_service):
    from openkb.agent.query import build_query_agent, iter_agent_response_events
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    configure(kb_dir, model_service, 10)
    model_service.replies.extend(
        [
            (200, streamed_response(tool_request(), "tool_calls")),
            (200, streamed_response({"role": "assistant", "content": "Winter: seven days."})),
        ]
    )
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir) as bundle:
        agent = build_query_agent(str(kb_dir / "wiki"), "openai/gpt-4o-mini", bundle=bundle)
        events = [
            e async for e in iter_agent_response_events(agent, "Read conditions", max_turns=2)
        ]
    final = next(e["data"] for e in events if e["event"] == "final")
    assert final["answer_outcome"] == "partial" and "turn_limit" in str(final["unfinished"])
    assert len(model_service.requests) == 2 and not model_service.requests[-1].get("tools")


@pytest.mark.asyncio
async def test_cancellation_after_original_read_never_saves_narration(kb_dir, model_service):
    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext

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
    model_service.replies.append((200, streamed_response(tool_request(), "tool_calls")))
    result = await ask_question(kb_dir, "Read the original.", save=True, context=context)
    assert read_finished(), events
    assert result.status == "stopped" and not result.saved_path and result.unfinished
    assert len(model_service.requests) == 1
    assert not any(e.get("event") == "final" for e in events)
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))


@pytest.mark.asyncio
async def test_long_conversation_preparation_preserves_the_only_answer_send(kb_dir, model_service):
    from openkb.agent.chat_session import ChatSession
    from openkb.application.conversations import continue_conversation
    from openkb.application.execution import ExecutionContext

    configure(kb_dir, model_service, 1)
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(9):
        session.record_turn(f"Preference {index}: " + "workspace " * 1100, "Display only.", [])
    model_service.replies.append(
        (
            200,
            streamed_response(
                {
                    "role": "assistant",
                    "content": "Earlier preferences could not be recalled within this budget.",
                }
            ),
        )
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
