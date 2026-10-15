"""Committed turns and real ChatIndex/ConDB state obey conversation lifecycle rules."""

import json

import pytest

from ultrakb.agent.chat_session import ChatSession, load_session

pytest_plugins = ("test_vendor_sdk",)


def test_committed_turn_identity_survives_retry_and_projections_are_not_authority(kb_dir):
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    attempt = session.begin_attempt("Original question", identity="attempt-one")
    session.record_turn("Original question", "Original answer", [], attempt_id=attempt)
    first = load_session(kb_dir, session.id).turns[0]
    assert first.order == 1
    assert first.user_text == "Original question"
    assert first.assistant_text == "Original answer"
    assert first.attempt_id == attempt
    assert first.turn_id and len(first.content_digest) == 64

    session.record_turn("Original question", "Original answer", [], attempt_id=attempt)
    session.begin_attempt("Cancelled question", identity="not-committed")
    restored = load_session(kb_dir, session.id)
    assert restored.turns == [first]
    assert restored.turn_count == 1
    assert restored.completed_attempts == [attempt]
    document = json.loads(restored.path.read_text())
    document["user_turns"] = ["Poisoned projection"]
    document["assistant_texts"] = ["Poisoned answer"]
    restored.path.write_text(json.dumps(document))
    assert load_session(kb_dir, session.id).user_turns == ["Original question"]
    assert load_session(kb_dir, session.id).assistant_texts == ["Original answer"]


def test_previous_conversation_format_is_rejected_without_migration(kb_dir):
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.save()
    data = session.to_dict()
    data.pop("format_version", None)
    data.pop("turns", None)
    session.path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="format.*new conversation"):
        load_session(kb_dir, session.id)


def test_real_chatindex_checkpoint_catches_up_only_committed_prefix(kb_dir, model_service):
    from test_llm_execution import executor_for

    from ultrakb.context.conversation import ConversationMemory
    from ultrakb.locks import kb_ingest_lock, session_lock

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    for index in range(3):
        session.record_turn(f"Preference {index}: quiet indoor workspace.", "ASSISTANT_POISON", [])
    session.begin_attempt("UNCOMMITTED_QUESTION")
    executor = executor_for(model_service, max_calls=30)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        first = ConversationMemory(session, executor).catch_up()
        assert first.indexed_turns == 3
        assert first.pending_turns == 0
        assert first.diagnostics == ()
    assert "ASSISTANT_POISON" not in json.dumps(model_service.requests)
    assert "UNCOMMITTED_QUESTION" not in json.dumps(model_service.requests)

    restarted = load_session(kb_dir, session.id)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        assert ConversationMemory(restarted, executor).catch_up().indexed_turns == 3
    assert len(model_service.requests) == 3
    restarted.record_turn("Correction: choose the garden room.", "Display-only answer", [])
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        assert ConversationMemory(restarted, executor).catch_up().indexed_turns == 4
    assert len(model_service.requests) == 4


def test_failed_chatindex_append_retains_checkpoint_and_deletion_clears_projection(
    kb_dir, model_service, monkeypatch
):
    from test_llm_execution import executor_for

    from ultrakb.application.sessions import delete_conversation
    from ultrakb.context.conversation import ConversationMemory
    from ultrakb.context.projection import open_runtime
    from ultrakb.locks import kb_ingest_lock, session_lock

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn("Choose indoor work.", "Display answer", [])
    executor = executor_for(model_service, max_calls=20, retries=0)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        initial = ConversationMemory(session, executor).catch_up()
    session.record_turn("Correction: use the garden.", "ASSISTANT_ONLY", [])

    # A persistence failure after genuine indexing must preserve the durable prefix.
    from contextdb import ConDB

    def reject_projection(*args, **kwargs):
        raise ValueError("Invalid projection transaction")

    with monkeypatch.context() as stopped:
        stopped.setattr(ConDB, "store", reject_projection)
        with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
            memory = ConversationMemory(session, executor)
            failed = memory.catch_up()
            assert (failed.indexed_turns, failed.pending_turns) == (1, 1)
            assert failed.tree_id == initial.tree_id
            assert any("index_lag" in item for item in failed.diagnostics)
            assert memory.state().indexed_turns == 1

    assert delete_conversation(kb_dir, session.id).status == "deleted"
    with pytest.raises(FileNotFoundError, match="deleted"):
        memory.catch_up()
    with open_runtime(kb_dir) as db:
        assert db.get_root_id(initial.tree_id) is None
        assert (
            db.conn.execute(
                "SELECT count(*) FROM okb_conversation_checkpoints WHERE session_id=?",
                (session.id,),
            ).fetchone()[0]
            == 0
        )


def test_catchup_stage_allowance_preserves_completed_prefix_for_next_round(kb_dir, model_service):
    from test_llm_execution import executor_for

    from ultrakb.context.conversation import ConversationMemory
    from ultrakb.locks import kb_ingest_lock, session_lock

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    for index in range(8):
        session.record_turn(f"Workspace preference {index}: quiet.", "Display only", [])
    executor = executor_for(model_service, max_calls=30)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        state = ConversationMemory(session, executor).catch_up()
        assert 0 < state.indexed_turns < 8
        assert state.pending_turns == 8 - state.indexed_turns
        assert executor.stage_calls == {"conversation_index": 6}
        assert len(model_service.requests) == 6
        durable_prefix = state.indexed_turns
        executor.begin_question()
        state = ConversationMemory(load_session(kb_dir, session.id), executor).catch_up()
        assert state.indexed_turns > durable_prefix
        while state.pending_turns:
            executor.begin_question()
            state = ConversationMemory(load_session(kb_dir, session.id), executor).catch_up()
        assert (state.indexed_turns, state.pending_turns) == (8, 0)
        assert 8 <= len(model_service.requests) <= 18


def test_cancelled_index_send_cannot_commit_a_late_checkpoint(kb_dir, model_service, monkeypatch):
    from test_llm_execution import executor_for

    from ultrakb.context.conversation import ConversationMemory
    from ultrakb.locks import LockCancelled, kb_ingest_lock, session_lock

    stopped = False
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn("Committed preference: quiet indoor workspace.", "Display", [])
    executor = executor_for(model_service, max_calls=20, cancelled=lambda: stopped)
    complete = executor.complete

    def stop_after_real_response(*args, **kwargs):
        nonlocal stopped
        delivered = complete(*args, **kwargs)
        stopped = True
        return delivered

    monkeypatch.setattr(executor, "complete", stop_after_real_response)
    with session_lock(kb_dir, session.id), kb_ingest_lock(kb_dir / ".ultrakb"):
        with pytest.raises(LockCancelled):
            ConversationMemory(session, executor).catch_up()
        stopped = False
        assert ConversationMemory(session, executor).state().indexed_turns == 0
    assert len(model_service.requests) == 1
