"""CLI, HTTP and the desktop's spawn runtime exercise the same real context chain."""

import asyncio
import json
import re

import pytest
from test_context_retrieval import rank_reply
from test_vendor_sdk import response, streamed_response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("entry", ["cli", "http", "http_stream", "desktop"])
def test_long_conversation_entrypoints_keep_users_and_all_stage_usage(
    kb_dir, model_service, monkeypatch, tmp_path, entry
):
    from agents import set_tracing_disabled

    from openkb.agent.chat_session import ChatSession, load_session
    from openkb.llm_usage import read_requests

    set_tracing_disabled(True)
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / "wiki/sources/manual.md").write_text("A retained original source.")
    session = ChatSession.new(kb_dir, "openai/gpt-4o-mini", "en")
    for index in range(9):
        preference = (
            "My room is ARCHIVE_ROOM and my budget is 42 credits. "
            if index == 0
            else "Correction: GARDEN_ROOM; retain my budget. "
            if index == 8
            else f"My workspace note {index}. "
        )
        session.record_turn(preference + "workspace " * 1100, "ASSISTANT_POISON: 999", [])

    def reply(request):
        prompt = json.dumps(request["messages"])
        assert "ASSISTANT_POISON" not in prompt
        if request.get("stream"):
            assert all(text in prompt for text in ("ARCHIVE_ROOM", "GARDEN_ROOM", "42 credits"))
            return streamed_response({"role": "assistant", "content": "GARDEN_ROOM, 42 credits."})
        if request.get("tools"):
            ids = re.findall(r"- id: (\S+)", request["messages"][-1]["content"])
            ranked = rank_reply(ids)
            ranked["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"ranked_ids": ids, "done": True}
            )
            return ranked
        value = (
            {"title": "Workspace"} if "Name the subject" in prompt else {"belongs_to_current": True}
        )
        return response({"role": "assistant", "content": json.dumps(value)})

    model_service.replies.extend((200, reply) for _ in range(8))
    question = "Confirm my room and budget."
    public = None
    if entry == "cli":
        from openkb.agent.chat import _build_style, _run_turn

        # The REPL delegates to the public application, including session reload.
        asyncio.run(
            _run_turn(
                session,
                question,
                style=_build_style(False),
                use_color=False,
                raw=True,
            )
        )
    elif entry.startswith("http"):
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        with TestClient(create_app()) as client:
            result = client.post(
                "/api/v1/chat",
                json={
                    "kb": "fixture",
                    "message": question,
                    "session_id": session.id,
                    "stream": entry == "http_stream",
                },
            )
        assert result.status_code == 200, result.text
        if entry == "http_stream":
            frames = [
                frame for frame in result.text.split("\n\n") if frame.startswith("event: final\n")
            ]
            assert len(frames) == 1, result.text
            public = json.loads(frames[0].split("data: ", 1)[1])
        else:
            public = result.json()
        assert public["answer"] == "GARDEN_ROOM, 42 credits."
    else:
        from dataclasses import asdict

        from openkb.runtime.requests import ContinueConversation
        from openkb.runtime.tasks import TaskManager

        manager = TaskManager(history_dir=tmp_path / "desktop-history", max_workers=1)
        try:
            task = manager.submit(
                kb_dir, [ContinueConversation(message=question, session_id=session.id)]
            )
            result = manager.wait(task, timeout=30)
            assert result.state == "completed", result
            assert result.processes_reaped
            public = asdict(result.results[0])
            assert public["output"] == "GARDEN_ROOM, 42 credits."
        finally:
            manager.shutdown(stop=True)
            assert manager.join(5)
    restored = load_session(kb_dir, session.id)
    assert restored.turns[:-1] == session.turns[:9]
    assert restored.turns[-1].user_text == question
    assert restored.turns[-1].assistant_text == "GARDEN_ROOM, 42 credits."
    requests = read_requests(kb_dir)
    assert len(requests) == len(model_service.requests) == 8
    assert {r.operation for r in requests} == {
        "conversation_index",
        "conversation_retrieval",
        "answer",
    }
    if public:
        assert public["model_usage"]["current"]["requests"] == 8
        assert public["context"]["index"]["indexed_turns"] == 5
        assert public["context"]["recall"]["status"] == "completed"
        assert public["usage"] == restored.token_usage
