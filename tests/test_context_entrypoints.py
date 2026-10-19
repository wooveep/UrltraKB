"""CLI, HTTP and the desktop's spawn runtime exercise the same real context chain."""

import asyncio
import json

import pytest

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("entry", ["cli", "http", "http_stream", "desktop"])
def test_conversation_entrypoints_keep_users_and_all_stage_usage(
    kb_dir, model_service, monkeypatch, tmp_path, entry
):
    from agents import set_tracing_disabled
    from conversation_fixtures import seed_conversation
    from native_usage_fixtures import read_requests

    from ultrakb.application.conversations import read_conversation
    from ultrakb.terminal_chat_state import TerminalConversation

    set_tracing_disabled(True)
    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/manual.md", "A retained original source.")
    turns = []
    for index in range(9):
        preference = (
            "My room is ARCHIVE_ROOM and my budget is 42 credits. "
            if index in {0, 3}
            else "Correction: GARDEN_ROOM; retain my budget. "
            if index == 8
            else f"My workspace note {index}. "
        )
        turns.append((preference, "ASSISTANT_POISON: 999"))
    session = seed_conversation(kb_dir, turns)

    question = "Using my preferences only, confirm my current room and budget in one sentence."
    public = None
    if entry == "cli":
        from ultrakb.agent.chat import _build_style, _run_turn

        # The REPL delegates to the public application, including session reload.
        asyncio.run(
            _run_turn(
                TerminalConversation.resume(kb_dir, session.id),
                question,
                style=_build_style(False),
                use_color=False,
                raw=True,
            )
        )
    elif entry.startswith("http"):
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app

        monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
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
        assert "GARDEN_ROOM" in public["answer"] and "42" in public["answer"]
    else:
        from dataclasses import asdict

        from ultrakb.runtime.requests import ContinueConversation
        from ultrakb.runtime.tasks import TaskManager

        manager = TaskManager(history_dir=tmp_path / "desktop-history", max_workers=1)
        try:
            task = manager.submit(
                kb_dir, [ContinueConversation(message=question, session_id=session.id)]
            )
            result = manager.wait(task, timeout=90)
            assert result.state == "completed", result
            assert result.processes_reaped
            public = asdict(result.results[0])
            assert "GARDEN_ROOM" in public["output"] and "42" in public["output"]
        finally:
            manager.shutdown(stop=True)
            assert manager.join(5)
    restored = read_conversation(kb_dir, session.id)
    assert restored.turns[:-1] == session.turns[:9]
    assert restored.turns[-1][0] == question
    assert "GARDEN_ROOM" in restored.turns[-1][1]
    assert "42" in restored.turns[-1][1]
    assert "999" not in restored.turns[-1][1]
    requests = read_requests(kb_dir)
    assert len(requests) == len(model_service.requests)
    assert 1 <= len(requests) <= 8
    assert "ASSISTANT_POISON" not in json.dumps(model_service.requests)
    assert "answer" in {r.operation for r in requests}
    assert {r.operation for r in requests} <= {"answer", "knowledge_retrieval"}
    if public:
        assert public["model_usage"]["current"]["requests"] == len(requests)
        assert public["context"]["recent_turns"] == 6
        assert public["context"]["status"] == "recent_only"
        assert public["usage"] == restored.usage
