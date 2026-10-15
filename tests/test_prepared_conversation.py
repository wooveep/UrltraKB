"""Application context uses user intent and preserves the real current submission."""

import asyncio
import json
import threading

import pytest

from ultrakb.agent.chat_session import load_session
from ultrakb.application.conversations import continue_conversation
from ultrakb.locks import atomic_write_text

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unknown_or_exceeded_capacity_stops_before_send_and_keeps_question(
    kb_dir, model_service, known
):
    from tokenizers import Tokenizer, models, pre_tokenizers

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "current": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = kb_dir / "fixture-tokenizer.json"
    tokenizer.save(str(path))
    capacity = (
        f"model_capacity:\n  max_input_tokens: 16\n  tokenizer_model: local:{path}\n"
        if known
        else ""
    )
    (kb_dir / ".ultrakb/config.yaml").write_text("model: openai/private-chat\n" + capacity)
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

    from ultrakb.agent.chat_session import ChatSession

    set_tracing_disabled(True)
    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained source.")
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    for index in range(8):
        session.record_turn(
            f"USER_INTENT_{index}: I prefer an indoor room.",
            "ASSISTANT_POISON",
            [
                {"role": "assistant", "content": "ASSISTANT_POISON"},
                {"type": "function_call_output", "call_id": "prior", "output": "TOOL_POISON"},
            ],
        )
    original = (
        "Using only my preferences, state my preferred room in one brief sentence. "
        "No research is needed."
    )
    result = await continue_conversation(kb_dir, original, session_id=session.id)
    assert result.status == "completed", result
    assert "indoor" in result.answer.lower()
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
async def test_threshold_recall_rereads_old_users_and_later_correction_wins(
    kb_dir, model_service, monkeypatch
):
    from agents import set_tracing_disabled

    from ultrakb.agent.chat_session import ChatSession
    from ultrakb.application.execution import ExecutionContext

    set_tracing_disabled(True)
    monkeypatch.setattr("ultrakb.context.preparation.RECALL_THRESHOLD_TOKENS", 1)
    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained source.")
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
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
        session.record_turn(text, "ASSISTANT_POISON: budget is 999", [])

    events = []
    result = await continue_conversation(
        kb_dir,
        "Confirm my room and budget.",
        session_id=session.id,
        context=ExecutionContext(on_event=events.append),
    )
    assert result.status == "completed", result
    assert "GARDEN_ROOM" in result.answer and "42" in result.answer
    assert "999" not in result.answer
    sent = json.dumps(model_service.requests)
    assert "ASSISTANT_POISON" not in sent and "budget is 999" not in sent
    assert all(term in sent for term in ("ARCHIVE_ROOM", "GARDEN_ROOM", "42 credits"))
    context = next(e["context"] for e in events if e.get("stage") == "context_preparation")
    assert context["index"]["indexed_turns"] == 5
    assert context["index"]["pending_turns"] == 4
    assert context["recall"]["status"] == "completed"
    assert 0 < context["recall"]["context_tokens"] <= 2000
    assert 0 < context["recall"]["stage_sends"]["retrieval"] <= 8
    restored = load_session(kb_dir, session.id)
    assert restored.turns[-1].user_text == "Confirm my room and budget."
    assert len(restored.turns) == 10
    from ultrakb.llm_usage import read_requests

    requests = read_requests(kb_dir)
    assert len(requests) == result.model_usage["current"]["requests"] == len(model_service.requests)
    assert 3 <= len(requests) <= 16
    assert {r.operation for r in requests} >= {
        "answer",
        "conversation_index",
        "conversation_retrieval",
    }
    assert result.usage["output"] == sum(r.output_total for r in requests)
    assert restored.token_usage == result.usage


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_cancel_during_real_context_work_keeps_submission_without_late_commit(
    kb_dir, model_service, cancel_task, monkeypatch
):
    from ultrakb.agent.chat_session import ChatSession
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import read_requests

    monkeypatch.setattr("ultrakb.context.preparation.RECALL_THRESHOLD_TOKENS", 1)

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained original.")
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    for index in range(9):
        session.record_turn(f"Intent {index}: quiet indoor workspace.", "POISON", [])
    model_service.delay = 0.2
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
    from ultrakb.application.conversations import ask_question

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 16\n  tokenizer_model: gpt-4o-mini\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained original.")
    result = await ask_question(kb_dir, "Explain.")
    assert result.status == "blocked", result
    assert "capacity" in result.error.lower()
    assert model_service.requests == []


@pytest.mark.asyncio
async def test_cancel_during_answer_settles_usage_without_committing(
    kb_dir, model_service, monkeypatch
):
    import litellm
    from agents import set_tracing_disabled
    from litellm import CustomStreamWrapper

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import read_requests

    receipts = []
    completion, acompletion = litellm.completion, litellm.acompletion
    stream_next = CustomStreamWrapper.__anext__

    def receive(response):
        if (usage := getattr(response, "usage", None)) is not None:
            cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
            receipts.append((response.id, usage.prompt_tokens, usage.completion_tokens, cached))

    def observed_completion(**kwargs):
        response = completion(**kwargs)
        receive(response)
        return response

    async def observed_acompletion(**kwargs):
        response = await acompletion(**kwargs)
        if not kwargs.get("stream"):
            receive(response)
        return response

    async def observed_chunk(stream):
        chunk = await stream_next(stream)
        receive(chunk)
        return chunk

    monkeypatch.setattr(litellm, "completion", observed_completion)
    monkeypatch.setattr(litellm, "acompletion", observed_acompletion)
    monkeypatch.setattr(CustomStreamWrapper, "__anext__", observed_chunk)

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained original.")
    set_tracing_disabled(True)
    events = []
    model_service.delay = 0.2
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
    requests = read_requests(kb_dir)
    assert len(requests) == len(model_service.requests) == len(receipts)
    assert sum(request.operation == "answer" for request in requests) == 1
    assert len({request.execution_id for request in requests}) == 1
    execution = json.loads(
        (kb_dir / f".ultrakb/usage/executions/{requests[0].execution_id}.json").read_text()
    )
    assert execution["state"] == "interrupted"
    for request in requests:
        assert request.state == "completed"
        assert request.usage_status == "known" and request.send_status == "sent"
        assert request.input_total is not None and request.input_total > 0
        assert request.output_total is not None and request.output_total > 0
        assert request.cached_input is not None
    assert sorted(
        (
            request.provider_request_id,
            request.input_total,
            request.output_total,
            request.cached_input,
        )
        for request in requests
    ) == sorted(receipts)
    assert restored.token_usage["input_cached"] == sum(request.cached_input for request in requests)
    assert restored.token_usage["input_uncached"] == sum(
        request.input_total - request.cached_input for request in requests
    )
    assert restored.token_usage["output"] == sum(request.output_total for request in requests)
    # After-turn cleanup completes the current response and its planned tools,
    # including any genuine retrieval request, before stopping the SDK loop.
    # The application discards that turn; every real transport receipt is billed.
