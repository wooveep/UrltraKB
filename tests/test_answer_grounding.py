"""Empty answers and rejected private drafts never become completed turns."""

import json

import pytest

from openkb.application.conversations import ask_question, continue_conversation


@pytest.mark.asyncio
async def test_empty_terminal_answer_never_completes(kb_dir, model_service):
    model_service.chat_response = lambda body: {"role": "assistant", "content": ""}
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Which port?", save=True)
    assert result.status != "completed" and result.saved_path is None
    assert result.usage["observable_attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", "<think>Rejected private draft</think>"])
async def test_empty_draft_is_removed_before_replacement_and_history(kb_dir, model_service, empty):
    calls = []

    def chat(body):
        calls.append(body)
        return {"role": "assistant", "content": empty if len(calls) == 1 else "Hello."}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    result = await continue_conversation(kb_dir, "Hello.")
    assert result.status == "completed" and result.answer == "Hello."
    assert len(calls) == 2
    from openkb.agent.chat_session import load_session

    saved = load_session(kb_dir, result.session_id)
    assert saved.assistant_texts == ["Hello."]
    for history in (saved.history, calls[1]["messages"]):
        assert not any(
            row.get("role") == "assistant" and row.get("content") == empty for row in history
        )
        assert "Rejected private draft" not in json.dumps(history)
