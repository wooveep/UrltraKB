"""Conversation maintenance through the shared application interface."""

from openkb.agent.chat_session import ChatSession, load_session


def test_confirmed_deletion_rejects_a_new_completed_turn(kb_dir):
    from openkb.application.conversations import read_conversation
    from openkb.application.execution import ExecutionContext
    from openkb.application.sessions import delete_conversation

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("First", "First answer", [])
    preview = read_conversation(kb_dir, session.id)
    session.record_turn("Second", "Second answer", [])
    context = ExecutionContext()
    conflict = delete_conversation(kb_dir, session.id, version=preview.version, context=context)
    assert conflict.status == "conflict"
    assert context.snapshot is None
    assert load_session(kb_dir, session.id).turn_count == 2

    latest = read_conversation(kb_dir, session.id)
    removed = delete_conversation(kb_dir, session.id, version=latest.version, context=context)
    assert removed.status == "deleted"
    assert context.snapshot is not None
    assert not session.path.exists()
    assert removed.changes == (f"deleted: .openkb/chats/{session.id}.json",)
    assert delete_conversation(kb_dir, session.id).status == "missing"


def test_malformed_conversation_cannot_redirect_its_identity(kb_dir):
    import json

    import pytest

    from openkb.agent.chat_session import list_sessions
    from openkb.application.conversations import read_conversation

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("Question", "Answer", [])
    data = session.to_dict()
    data["id"] = "another-conversation"
    session.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_conversation(kb_dir, session.id)
    assert list_sessions(kb_dir) == []


def test_transcript_exports_latest_completed_history_to_unique_copies(kb_dir):
    from pathlib import Path

    from openkb.application.sessions import export_conversation

    session = ChatSession.new(kb_dir, 'openai/model"quoted', "zh")
    session.record_turn("First question", "Answer with [[concepts/missing]].", [])
    old = load_session(kb_dir, session.id)
    session.record_turn("Second question", "Completed second answer", [])
    first = export_conversation(kb_dir, old, unique=True)
    second = export_conversation(kb_dir, session.id, unique=True)
    assert first.status == second.status == "exported"
    assert first.resources != second.resources
    text = Path(first.resources[0]).read_text()
    assert "Second question" in text
    assert "Completed second answer" in text
    assert "[[concepts/missing]]" not in text
    assert text == Path(second.resources[0]).read_text()
    assert load_session(kb_dir, session.id).turn_count == 2


def test_unchanged_windows_conversation_can_be_deleted_after_confirmation(kb_dir):
    from openkb.application.conversations import read_conversation
    from openkb.application.sessions import delete_conversation

    session = ChatSession.new(kb_dir, "openai/test", "en")
    session.record_turn("Question", "Answer", [])
    session.path.write_bytes(session.path.read_bytes().replace(b"\n", b"\r\n"))
    preview = read_conversation(kb_dir, session.id)
    assert delete_conversation(kb_dir, session.id, version=preview.version).status == "deleted"


def test_malformed_image_arguments_do_not_hide_completed_history(kb_dir):
    import json

    from openkb.agent.chat_session import list_sessions
    from openkb.application.conversations import read_conversation

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("Question", "Answer", [])
    data = session.to_dict()
    data["history"] = [{"type": "function_call", "name": "get_image", "arguments": "[]"}]
    session.path.write_text(json.dumps(data))
    assert list_sessions(kb_dir)[0]["id"] == session.id
    assert read_conversation(kb_dir, session.id).turns == (("Question", "Answer"),)
