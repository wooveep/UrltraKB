"""Application context uses user intent and preserves the real current submission."""

import asyncio
import json
import threading

import pytest

from openkb.agent.chat_session import load_session
from openkb.application.conversations import continue_conversation

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unknown_or_exceeded_capacity_stops_before_send_and_keeps_question(
    kb_dir, model_service, known
):
    from tokenizers import Tokenizer, models, pre_tokenizers

    from openkb.locks import atomic_write_text

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "current": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = kb_dir / "fixture-tokenizer.json"
    tokenizer.save(str(path))
    capacity = (
        f"model_capacity:\n  max_input_tokens: 16\n  tokenizer_model: local:{path}\n"
        if known
        else ""
    )
    (kb_dir / ".openkb/config.yaml").write_text("model: openai/private-chat\n" + capacity)
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained source.")
    question = "current " * 40
    result = await continue_conversation(kb_dir, question)
    assert result.status == "blocked", result
    assert "capacity" in result.error.lower()
    assert model_service.requests == []
    session = load_session(kb_dir, result.session_id)
    assert session.turns == []
    assert session.incomplete[0]["message"] == question


@pytest.mark.asyncio
async def test_recent_input_uses_six_real_user_turns_and_preserves_full_conversation(
    kb_dir, model_service
):
    from agents import set_tracing_disabled
    from test_vendor_sdk import streamed_response

    from openkb.agent.chat_session import ChatSession
    from openkb.locks import atomic_write_text

    set_tracing_disabled(True)
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained source.")
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(8):
        session.record_turn(
            f"USER_INTENT_{index}: I prefer an indoor room.",
            "ASSISTANT_POISON",
            [
                {"role": "assistant", "content": "ASSISTANT_POISON"},
                {"type": "function_call_output", "call_id": "prior", "output": "TOOL_POISON"},
            ],
        )
    original = "What should I consider for my preferred room?"
    model_service.replies.append(
        (
            200,
            streamed_response(
                {"role": "assistant", "content": "Keep indoor requirements in mind."}
            ),
        )
    )
    result = await continue_conversation(kb_dir, original, session_id=session.id)
    assert result.status == "completed", result
    assert len(model_service.requests) == 1
    sent = model_service.requests[0]["messages"]
    assert [m["role"] for m in sent if m["role"] != "system"] == ["user"]
    text = json.dumps(sent)
    assert "ASSISTANT_POISON" not in text and "TOOL_POISON" not in text
    assert "USER_INTENT_0" not in text and "USER_INTENT_1" not in text
    assert all(f"USER_INTENT_{index}" in text for index in range(2, 8))
    assert all(turn.turn_id in text for turn in session.turns[-6:])
    restored = load_session(kb_dir, session.id)
    assert restored.turns[-1].user_text == original
    assert restored.turns[-1].assistant_text == result.answer
    assert restored.turns[:-1] == session.turns


@pytest.mark.asyncio
async def test_threshold_recall_rereads_old_users_and_later_correction_wins(kb_dir, model_service):
    import re

    from agents import set_tracing_disabled
    from test_context_retrieval import rank_reply
    from test_vendor_sdk import response, streamed_response

    from openkb.agent.chat_session import ChatSession
    from openkb.application.execution import ExecutionContext
    from openkb.locks import atomic_write_text

    set_tracing_disabled(True)
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained source.")
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(9):
        text = (
            "My room is ARCHIVE_ROOM and my budget is 42 credits. "
            if index == 0
            else (
                "Correction: choose GARDEN_ROOM; keep my original budget. "
                if index == 8
                else f"Workspace notes {index}. "
            )
        )
        session.record_turn(text + "workspace " * 1100, "ASSISTANT_POISON: budget is 999", [])

    def model_reply(request):
        prompt = json.dumps(request["messages"])
        assert "ASSISTANT_POISON" not in prompt
        if request.get("stream"):
            assert "ARCHIVE_ROOM" in prompt and "GARDEN_ROOM" in prompt
            assert "42 credits" in prompt and "budget is 999" not in prompt
            return streamed_response(
                {"role": "assistant", "content": "GARDEN_ROOM, budget 42 credits."}
            )
        if request.get("tools"):
            ids = re.findall(r"- id: (\S+)", request["messages"][-1]["content"])
            reply = rank_reply(ids)
            reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"ranked_ids": ids, "done": True}
            )
            return reply
        value = (
            {"title": "Workspace"} if "Name the subject" in prompt else {"belongs_to_current": True}
        )
        return response({"role": "assistant", "content": json.dumps(value)})

    model_service.replies.extend((200, model_reply) for _ in range(9))
    events = []
    result = await continue_conversation(
        kb_dir,
        "Confirm my room and budget.",
        session_id=session.id,
        context=ExecutionContext(on_event=events.append),
    )
    assert result.status == "completed", result
    assert result.answer == "GARDEN_ROOM, budget 42 credits."
    context = next(e["context"] for e in events if e.get("stage") == "context_preparation")
    assert context["index"]["indexed_turns"] == 5
    assert context["index"]["pending_turns"] == 4
    assert context["recall"]["status"] == "completed"
    assert 0 < context["recall"]["context_tokens"] <= 2000
    assert context["recall"]["stage_sends"]["retrieval"] == 2
    restored = load_session(kb_dir, session.id)
    assert restored.turns[-1].user_text == "Confirm my room and budget."
    assert len(restored.turns) == 10
    from openkb.llm_usage import read_requests

    requests = read_requests(kb_dir)
    assert len(requests) == result.model_usage["current"]["requests"] == 9
    assert {r.operation for r in requests} == {
        "answer",
        "conversation_index",
        "conversation_retrieval",
    }
    assert result.usage["output"] == sum(r.output_total for r in requests)
    assert restored.token_usage == result.usage


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_cancel_during_real_context_work_keeps_submission_without_late_commit(
    kb_dir, model_service, cancel_task
):
    from test_vendor_sdk import response

    from openkb.agent.chat_session import ChatSession
    from openkb.application.execution import ExecutionContext
    from openkb.llm_usage import read_requests

    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / "wiki/sources/manual.md").write_text("A retained original.")
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(9):
        session.record_turn(f"Intent {index}: " + "workspace " * 1100, "POISON", [])
    model_service.delay = 0.2
    model_service.replies.append(
        (200, response({"role": "assistant", "content": '{"title":"Notes"}'}))
    )
    stopped = threading.Event()
    task = asyncio.create_task(
        continue_conversation(
            kb_dir,
            "Retain this whole current question",
            session_id=session.id,
            context=ExecutionContext(cancelled=stopped.is_set),
        )
    )
    for _ in range(100):
        if model_service.requests:
            break
        await asyncio.sleep(0.01)
    assert len(model_service.requests) == 1
    if cancel_task:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        stopped.set()
        result = await task
        assert result.status == "stopped"
        assert result.model_usage["current"]["requests"] == 1
    restored = load_session(kb_dir, session.id)
    assert restored.turns == session.turns
    assert restored.incomplete[0]["message"] == "Retain this whole current question"
    assert len(model_service.requests) == len(read_requests(kb_dir)) == 1
    # This synchronous HTTP response finished and was billed after stop. The
    # application discards it and waits for the worker; transport facts stay true.
    assert read_requests(kb_dir)[0].state == "completed"


@pytest.mark.asyncio
async def test_current_question_fits_but_reader_schemas_exceed_capacity(kb_dir, model_service):
    from openkb.application.conversations import ask_question

    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 16\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / "wiki/sources/manual.md").write_text("A retained original.")
    result = await ask_question(kb_dir, "Explain.")
    assert result.status == "blocked", result
    assert "capacity" in result.error.lower()
    assert model_service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("report_usage", [False, True])
async def test_cancel_during_answer_settles_usage_without_committing(
    kb_dir, model_service, report_usage
):
    from agents import set_tracing_disabled
    from test_vendor_sdk import streamed_response

    from openkb.application.execution import ExecutionContext
    from openkb.llm_usage import read_requests

    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / "wiki/sources/manual.md").write_text("A retained original.")
    set_tracing_disabled(True)
    events = []
    model_service.delay = 0.2
    chunks = streamed_response({"role": "assistant", "content": "ok"})
    if not report_usage:
        chunks = [chunk for chunk in chunks if not chunk.get("usage")]
    model_service.replies.append((200, chunks))
    task = asyncio.create_task(
        continue_conversation(
            kb_dir, "Retain my submission", context=ExecutionContext(on_event=events.append)
        )
    )
    for _ in range(100):
        if model_service.requests:
            break
        await asyncio.sleep(0.01)
    assert len(model_service.requests) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    identity = next(event["session_id"] for event in events if event.get("stage") == "answering")
    restored = load_session(kb_dir, identity)
    assert restored.turns == []
    assert restored.incomplete[0]["message"] == "Retain my submission"
    assert restored.token_usage is not None
    request = read_requests(kb_dir)[0]
    assert restored.token_usage["output"] == request.output_total == (3 if report_usage else None)
    # Managed after-turn cleanup settled the HTTP response while the application
    # discarded the answer. The ledger reports transport completion honestly.
    assert request.state == "completed"
