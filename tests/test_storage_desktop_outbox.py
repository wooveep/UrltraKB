"""Interrupted desktop submissions recover into native history without replaying work."""

import os
from threading import Event, Thread

import pytest

from ultrakb.application.conversations import read_conversation
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.sessions import list_conversations
from ultrakb.desktop.chat_outbox import ChatOutbox
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.session_catalog import SessionCatalog


def test_recovered_outbox_preserves_one_pending_original_across_failed_file_ack(
    tmp_path, monkeypatch
):
    root = tmp_path / "kb"
    initialize_kb(root, seed_environment=False)
    outbox = ChatOutbox(tmp_path / "outbox")
    submission = outbox.accept(root, "pending", "Stopped before the first model call.", new=True)
    (root / ".ultrakb/config.yaml").unlink()

    def failed_ack(*args):
        raise OSError("injected outbox acknowledgement failure")

    with monkeypatch.context() as fault:
        fault.setattr(outbox, "discard", failed_ack)
        with pytest.raises(OSError):
            outbox.recover(root)
    first = read_conversation(root, "pending")
    assert first.turns == () and first.incomplete == ((0, "Stopped before the first model call."),)
    assert not (root / ".ultrakb/chats").exists()
    assert (outbox._directory(root) / (submission.id + ".json")).exists()
    outbox.recover(root)
    assert read_conversation(root, "pending") == first
    assert not (outbox._directory(root) / (submission.id + ".json")).exists()


def test_outbox_recovery_does_not_resurrect_a_deleted_conversation(tmp_path):
    root = tmp_path / "kb"
    initialize_kb(root, seed_environment=False)
    outbox = ChatOutbox(tmp_path / "outbox")
    outbox.accept(root, "deleted", "First stopped question", new=True)
    outbox.recover(root)
    viewed = read_conversation(root, "deleted")
    identity = resolve_scope(root).knowledge.identity
    assert (
        SessionCatalog(root, identity).delete("deleted", view_id="legacy", expected=viewed.version)
        == "deleted"
    )
    outbox.accept(root, "deleted", "Another stopped question", new=True)
    outbox.recover(root)
    assert list_conversations(root) == []
    with pytest.raises(FileNotFoundError):
        read_conversation(root, "deleted")


def test_outbox_rejects_a_replaced_database_even_when_the_kb_directory_is_unchanged(tmp_path):
    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root, seed_environment=False)
    outbox = ChatOutbox(tmp_path / "outbox")
    outbox.accept(root, "old-generation", "A question for the previous store", new=True)
    initialize_kb(replacement, seed_environment=False)
    os.replace(root / ".ultrakb", tmp_path / "previous-state")
    os.replace(replacement / ".ultrakb", root / ".ultrakb")
    outbox.recover(root)
    assert list_conversations(root) == []
    assert not list(outbox._directory(root).glob("*.json"))


def test_desktop_history_load_keeps_its_captured_store_identity(tmp_path):
    from types import SimpleNamespace

    from ultrakb.desktop.conversations import Chat, Conversations
    from ultrakb.storage.session_store import SessionStore

    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root, seed_environment=False)
    scope = resolve_scope(root)
    initialize_kb(replacement, seed_environment=False)
    new_scope = resolve_scope(replacement)
    SessionStore(replacement, new_scope.knowledge.identity).create(
        "same-session", model="gpt-4o-mini", language="en"
    )
    SessionStore(replacement, new_scope.knowledge.identity).append_exchange(
        "same-session", "new", "Question from another store", "Answer from another store"
    )
    os.replace(root / ".ultrakb", tmp_path / "old-state")
    os.replace(replacement / ".ultrakb", root / ".ultrakb")

    class ImmediateIO:
        def submit(self, operation, loaded, **options):
            try:
                value = operation()
            except Exception as error:
                loaded(None, error)
            else:
                loaded(value, None)

    controller = Conversations.__new__(Conversations)
    controller.window = SimpleNamespace(
        scope=scope, kb=root, _quitting=False, _deleting_kbs=set(), io=ImmediateIO()
    )
    controller.active = None
    controller.update_controls = lambda: None
    chat = Chat(root, "same-session", persisted=True)
    controller.load(chat)
    assert chat.read_error and chat.turns == []


def test_recovery_waiting_for_a_session_does_not_block_new_desktop_admission(tmp_path):
    from ultrakb.locks import session_lock

    root = tmp_path / "kb"
    initialize_kb(root, seed_environment=False)
    outbox = ChatOutbox(tmp_path / "outbox")
    outbox.accept(root, "busy", "A stopped question", new=True)
    identity = resolve_scope(root).knowledge.identity
    locked, release, waiting, admitted = Event(), Event(), Event(), Event()
    errors = []

    def holder():
        with session_lock(root, "busy", database_identity=identity):
            locked.set()
            release.wait(5)

    def recover():
        try:
            outbox.recover(root, on_wait=waiting.set)
        except Exception as error:
            errors.append(error)

    def accept():
        try:
            outbox.accept(root, "independent", "A new question", new=True)
            admitted.set()
        except Exception as error:
            errors.append(error)

    holder_thread, recovery_thread, admission_thread = (
        Thread(target=holder),
        Thread(target=recover),
        Thread(target=accept),
    )
    holder_thread.start()
    try:
        assert locked.wait(2)
        recovery_thread.start()
        assert waiting.wait(2)
        admission_thread.start()
        assert admitted.wait(2), errors
    finally:
        release.set()
        for thread in (holder_thread, recovery_thread, admission_thread):
            if thread.ident is not None:
                thread.join(3)
    assert not errors
    assert all(
        not thread.is_alive() for thread in (holder_thread, recovery_thread, admission_thread)
    )
