"""Actual chat tools publish bounded independent outputs against their frozen base."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation
from ultrakb.application.views import storage_scope
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def write_call(identity, path, content):
    return {
        "tool_calls": [
            {
                "index": 0,
                "id": identity,
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": path, "content": content}),
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_chat_tool_writes_advance_their_private_base_and_keep_fixed_evidence(tmp_path):
    root, identity, publication = question_knowledge(tmp_path)
    path = "wiki/explorations/note.md"
    with StreamTransport(
        [
            write_call("write-one", path, "First accepted note."),
            write_call("write-two", path, "Second accepted note."),
            {"content": "The value is 17. See [[sources/fixture]]."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write a note twice.",
            new_session_id="tool-writes",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    bound = KnowledgeStore(root, identity).bind()
    assert bound.read("explorations/note.md") == b"Second accepted note."
    assert bound.publication_id != publication.publication_id
    assert result.resources == (str(root / path),)
    assert result.changes == (
        "created: wiki/explorations/note.md",
        "updated: wiki/explorations/note.md",
        "saved turn: tool-writes",
    )
    assert result.citations["references"][0]["publication_id"] == publication.publication_id
    assert "Written: " + path in json.dumps(service.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_persisted_stop_prevents_a_late_tool_publication(tmp_path, monkeypatch):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    publish = KnowledgeStore.publish

    def stopped_before_commit(store, prepared, **kwargs):
        ExecutionStore(root, identity).request_stop(context.storage_execution.execution_id)
        return publish(store, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "publish", stopped_before_commit)
    with StreamTransport(
        [write_call("late-write", "wiki/explorations/late.md", "Late note."), {"content": "Done."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write a note.",
            new_session_id="stopped-tool",
            scope=storage_scope(root, identity),
            context=context,
        )
    assert KnowledgeStore(root, identity).bind().publication_id == publication.publication_id
    assert result.status == "stopped", result.error
    assert result.resources == ()


@pytest.mark.asyncio
async def test_accepted_note_survives_failed_turn_and_its_answer_resumes_without_resending(
    tmp_path,
):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.session_store import SessionStore

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_turn BEFORE INSERT ON ukb_turns "
            "BEGIN SELECT RAISE(ABORT, 'Turn commit unavailable'); END"
        )
    arguments = {"new_session_id": "failed-written-turn", "attempt_id": "stable-written-intent"}
    path = "wiki/explorations/durable.md"
    with StreamTransport(
        [write_call("save-note", path, "Independently accepted note."), {"content": "Full answer."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await continue_conversation(
            root, "Write a durable note.", scope=storage_scope(root, identity), **arguments
        )
        assert failed.status == "failed", failed.error
        assert failed.resources == (str(root / path),)
        assert SessionStore(root, identity).turns("failed-written-turn") == []
        assert KnowledgeStore(root, identity).bind().read("explorations/durable.md") == (
            b"Independently accepted note."
        )
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_turn")
        resumed = await continue_conversation(
            root, "Write a durable note.", scope=storage_scope(root, identity), **arguments
        )
    assert resumed.status == "completed" and resumed.answer == "Full answer.", resumed.error
    assert resumed.resources == failed.resources
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_unconfirmed_tool_failure_cannot_be_hidden_by_a_later_final_answer(
    tmp_path, monkeypatch
):
    import sqlite3

    root, identity, publication = question_knowledge(tmp_path)

    def unavailable(store, prepared, **kwargs):
        raise sqlite3.OperationalError("Publication storage unavailable")

    monkeypatch.setattr(KnowledgeStore, "publish", unavailable)
    with StreamTransport(
        [
            write_call("failed-note", "wiki/explorations/unconfirmed.md", "Candidate."),
            {"content": "Done."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write a note.",
            new_session_id="unconfirmed-note",
            scope=storage_scope(root, identity),
        )
    assert result.status == "blocked", result.error
    assert result.resources == () and result.turn_count == 0
    assert KnowledgeStore(root, identity).bind().publication_id == publication.publication_id


@pytest.mark.asyncio
async def test_note_writes_release_coordination_before_the_next_actual_model_wait(tmp_path):
    import asyncio
    from threading import Event

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.maintenance import Maintenance

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db:
        put_blob(db, b"unrelated orphan during the next model wait")
    waiting, release, stopped = Event(), Event(), Event()
    context = ExecutionContext(storage_identity=identity, cancelled=stopped.is_set)

    def response(request, index):
        if index == 0:
            return write_call(
                "write-before-wait", "wiki/explorations/before-wait.md", "Keep this note."
            )
        waiting.set()
        release.wait(5)
        return {"content": "An answer arriving after cancellation."}

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(
            continue_conversation(
                root,
                "Write a note, then think.",
                new_session_id="write-then-wait",
                scope=storage_scope(root, identity),
                context=context,
            )
        )
        try:
            assert await asyncio.to_thread(waiting.wait, 3)
            collection = await asyncio.wait_for(
                asyncio.to_thread(Maintenance(root, identity).collect), timeout=2
            )
            assert collection.status == "completed" and collection.deleted_count == 1
            stopped.set()
        finally:
            release.set()
        result = await task
    assert result.status == "stopped", result.error
    assert result.resources == (str(root / "wiki/explorations/before-wait.md"),)
    assert (
        KnowledgeStore(root, identity).bind().read("explorations/before-wait.md")
        == b"Keep this note."
    )


@pytest.mark.asyncio
async def test_a_later_conflict_keeps_the_exact_receipt_for_an_earlier_accepted_note(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    accepted = None

    def response(request, index):
        nonlocal accepted
        if index == 0:
            return write_call("first-note", "wiki/explorations/first.md", "First accepted content.")
        if index == 1:
            knowledge = KnowledgeStore(root, identity)
            accepted = knowledge.bind()
            knowledge.publish(knowledge.prepare_changes(accepted, {"explorations/first.md": None}))
            return write_call("second-note", "wiki/explorations/second.md", "Conflicting content.")
        return {"content": "Done."}

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root, "Write two notes.", new_session_id="later-conflict", scope=scope
        )
    assert result.status == "blocked", result.error
    assert result.resources == (str(root / "wiki/explorations/first.md"),)
    assert result.result_publication_id == accepted.publication_id
    assert (
        KnowledgeStore(root, identity)
        .bind(result.result_publication_id)
        .read("explorations/first.md")
        == b"First accepted content."
    )


@pytest.mark.asyncio
async def test_a_concurrent_publication_retains_the_model_note_for_review(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    other = None

    def response(request, index):
        nonlocal other
        if index == 0:
            knowledge = KnowledgeStore(root, identity)
            other = knowledge.publish(
                knowledge.prepare_changes(knowledge.bind(), {"concepts/other.md": b"Other edit."})
            )
            return write_call(
                "conflicting-write", "wiki/explorations/conflict.md", "Retain this note."
            )
        return {"content": "A final answer cannot hide the write conflict."}

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write while another editor publishes.",
            new_session_id="write-conflict",
            scope=scope,
        )
    assert result.status == "blocked", result.error
    assert result.reason_code == "knowledge_changed" and result.candidate_revision_id is not None
    assert KnowledgeStore(root, identity).bind().publication_id == other.publication_id
    assert result.resources == ()


@pytest.mark.asyncio
async def test_copied_tool_context_cannot_write_after_the_actual_worker_settles(
    tmp_path, monkeypatch
):
    from contextvars import copy_context

    from ultrakb.agent.tools import write_kb_file

    root, identity, _ = question_knowledge(tmp_path)
    scope, copied = storage_scope(root, identity), []
    publish = KnowledgeStore.publish

    def captured(store, prepared, **kwargs):
        copied.append(copy_context())
        return publish(store, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "publish", captured)
    with StreamTransport(
        [
            write_call("save-note", "wiki/explorations/accepted.md", "Accepted."),
            {"content": "Done."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root, "Write a note.", new_session_id="revoked-writer", scope=scope
        )
    assert result.status == "completed", result.error
    before = KnowledgeStore(root, identity).bind().publication_id
    with pytest.raises(RuntimeError, match="finished"):
        copied[0].run(
            write_kb_file, "wiki/explorations/too-late.md", "Late callback.", str(root), scope=scope
        )
    assert KnowledgeStore(root, identity).bind().publication_id == before


@pytest.mark.asyncio
async def test_lost_publication_acknowledgement_does_not_hide_an_accepted_tool_note(
    tmp_path, monkeypatch
):
    root, identity, _ = question_knowledge(tmp_path)
    publish = KnowledgeStore.publish

    def lost_reply(store, prepared, **kwargs):
        publish(store, prepared, **kwargs)
        raise OSError("Publication reply was lost")

    monkeypatch.setattr(KnowledgeStore, "publish", lost_reply)
    path = "wiki/explorations/acknowledged.md"
    with StreamTransport(
        [
            write_call("accepted-write", path, "Accepted before the lost reply."),
            {"content": "Done."},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Write a note.",
            new_session_id="publication-receipt",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    assert result.resources == (str(root / path),)
    assert result.result_publication_id == KnowledgeStore(root, identity).bind().publication_id
    assert len(service.requests) == 2
