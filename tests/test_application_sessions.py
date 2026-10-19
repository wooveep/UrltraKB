"""Shared native session maintenance retains concurrent history and unique exports."""

from pathlib import Path

import pytest

from ultrakb.application.conversations import read_conversation
from ultrakb.application.sessions import delete_conversation, export_conversation
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.session_store import SessionStore


def _session(root):
    scope = resolve_scope(root)
    store = SessionStore(root, scope.knowledge.identity)
    store.create("native-session", model='openai/model"quoted', language="zh")
    return store, "native-session"


def test_confirmed_deletion_rejects_a_new_completed_turn(kb_dir):
    store, session = _session(kb_dir)
    store.append_exchange(session, "first", "First", "First answer")
    preview = read_conversation(kb_dir, session)
    store.append_exchange(session, "second", "Second", "Second answer")
    conflict = delete_conversation(kb_dir, session, version=preview.version)
    assert conflict.status == "conflict"
    assert len(store.load(session).turns) == 2
    latest = read_conversation(kb_dir, session)
    assert delete_conversation(kb_dir, session, version=latest.version).status == "deleted"
    with pytest.raises(FileNotFoundError):
        read_conversation(kb_dir, session)
    assert delete_conversation(kb_dir, session).status == "missing"


def test_transcript_exports_latest_completed_history_to_unique_copies(kb_dir):
    store, session = _session(kb_dir)
    store.append_exchange(session, "first", "First question", "Answer with [[concepts/missing]].")
    old = read_conversation(kb_dir, session)
    store.append_exchange(session, "second", "Second question", "Completed second answer")
    first = export_conversation(kb_dir, session, unique=True)
    second = export_conversation(kb_dir, session, unique=True)
    assert first.status == second.status == "exported"
    assert first.resources != second.resources
    text = Path(first.resources[0]).read_text()
    assert "Second question" in text and "Completed second answer" in text
    assert "[[concepts/missing]]" not in text
    assert text == Path(second.resources[0]).read_text()
    assert len(old.turns) == 1 and len(store.load(session).turns) == 2
