"""Main-store session history and management through the shared application API."""

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation, read_conversation
from ultrakb.application.sessions import (
    delete_conversation,
    list_conversations,
    resolve_conversation,
)
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_history_reads_original_answers_and_versioned_delete_keeps_concurrent_work(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport(
        [{"content": "First answer. See [[sources/fixture]]."}, {"content": "Second answer."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await continue_conversation(
            root,
            "First original question.",
            new_session_id="managed-history",
            scope=storage_scope(root, identity),
        )
        assert first.status == "completed", first.error
        scope = storage_scope(root, identity)
        preview = read_conversation(root, first.session_id, scope=scope)
        assert preview.turns == (("First original question.", first.answer),)
        assert preview.answers[0].citations == first.citations
        assert list_conversations(root, scope=scope)[0]["id"] == first.session_id
        assert resolve_conversation(root, "managed", scope=scope) == first.session_id
        second = await continue_conversation(
            root,
            "Second original question.",
            session_id=first.session_id,
            scope=scope,
        )
        assert second.status == "completed", second.error
    conflict = delete_conversation(root, first.session_id, version=preview.version, scope=scope)
    assert conflict.status == "conflict"
    latest = read_conversation(root, first.session_id, scope=scope)
    assert len(latest.turns) == 2
    assert (
        delete_conversation(root, first.session_id, version=latest.version, scope=scope).status
        == "deleted"
    )
    assert delete_conversation(root, first.session_id, scope=scope).status == "missing"
    assert list_conversations(root, scope=scope) == []
    with pytest.raises(FileNotFoundError):
        read_conversation(root, first.session_id, scope=scope)


def test_history_paging_does_not_read_older_originals_and_list_uses_bounded_cursors(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from ultrakb.storage.session_store import SessionStore

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    store = SessionStore(root, identity)
    for name in ("history-a", "history-b", "history-c"):
        store.create(name, model="gpt-4o-mini", language="en")
    for ordinal in range(70):
        store.append_exchange("history-a", f"turn-{ordinal}", f"Q{ordinal}", f"A{ordinal}")
    first = list_conversations(root, scope=scope, limit=2)
    second = list_conversations(root, scope=scope, limit=2, after=first[-1]["cursor"])
    assert [entry["id"] for entry in first + second] == ["history-a", "history-c", "history-b"]
    assert resolve_conversation(root, "__latest__", scope=scope) == "history-a"
    assert resolve_conversation(root, "history-b", scope=scope) == "history-b"
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_conversation(root, "history-", scope=scope)

    read = Path.read_bytes

    def bounded_original(path):
        payload = read(path)
        if b'"ordinal":' in payload:
            import json

            assert json.loads(payload)["ordinal"] >= 60, "Unselected history was loaded"
        return payload

    monkeypatch.setattr(Path, "read_bytes", bounded_original)
    latest = read_conversation(root, "history-a", scope=scope, limit=5)
    previous = read_conversation(
        root, "history-a", scope=scope, limit=5, before=latest.older_before
    )
    assert latest.total_turns == previous.total_turns == 70
    assert latest.start_ordinal == 65 and latest.older_before == 65
    assert latest.turns == tuple((f"Q{i}", f"A{i}") for i in range(65, 70))
    assert previous.start_ordinal == 60 and previous.older_before == 60
    assert previous.turns == tuple((f"Q{i}", f"A{i}") for i in range(60, 65))


def test_pending_submissions_are_ordered_paged_and_invalidate_delete_confirmation(tmp_path):
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.session_store import SessionStore

    root, identity, publication = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    store = SessionStore(root, identity)
    store.create("pending", model="gpt-4o-mini", language="en")
    preview = read_conversation(root, "pending", scope=scope)
    for order in (30, 10, 20):
        execution = ExecutionStore(root, identity).begin(
            "conversation", fingerprint="pending-fixture", max_calls=0
        )
        store.start_submission(
            "pending",
            f"submission-{order}",
            f"Original pending question {order}.",
            execution_id=execution.execution_id,
            publication_id=publication.publication_id,
            order=order,
        )
    first = read_conversation(root, "pending", scope=scope, limit=2)
    second = read_conversation(
        root, "pending", scope=scope, limit=2, pending_after=first.next_pending
    )
    assert first.incomplete == (
        (0, "Original pending question 10."),
        (0, "Original pending question 20."),
    )
    assert second.incomplete == ((0, "Original pending question 30."),)
    assert first.next_pending is not None and second.next_pending is None
    assert (
        delete_conversation(root, "pending", version=preview.version, scope=scope).status
        == "conflict"
    )


@pytest.mark.asyncio
async def test_history_remains_readable_during_a_real_model_wait(tmp_path):
    import asyncio
    from threading import Event

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with StreamTransport([{"content": "Committed original answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await continue_conversation(
            root, "First question.", new_session_id="read-while-busy", scope=scope
        )
    assert first.status == "completed", first.error
    gate = Event()
    with StreamTransport([{"content": "Later answer."}], gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(
            continue_conversation(
                root, "Waiting question.", session_id=first.session_id, scope=scope
            )
        )
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            history = await asyncio.wait_for(
                asyncio.to_thread(read_conversation, root, first.session_id, scope=scope), timeout=2
            )
            assert history.turns == (("First question.", "Committed original answer."),)
            assert history.incomplete == ((1, "Waiting question."),)
            assert history.total_turns == 1
        finally:
            gate.set()
        result = await task
    assert result.status == "completed" and result.turn_count == 2, result.error


@pytest.mark.asyncio
async def test_history_trace_references_the_original_assistant_without_storing_it_twice(
    tmp_path, monkeypatch
):
    from pathlib import Path

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    answer = "ASSISTANT_BODY_987: the value is 17. See [[sources/fixture]]."
    responses = [
        {
            "content": "Opening the source.",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "read-source",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/fixture.md"}'},
                }
            ],
        },
        {"content": answer},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root, "Read the source.", new_session_id="trace-reference", scope=scope
        )
    assert result.status == "completed", result.error
    read, payloads = Path.read_bytes, []

    def captured(path):
        payload = read(path)
        if "blobs" in path.parts:
            payloads.append(payload)
        return payload

    monkeypatch.setattr(Path, "read_bytes", captured)
    history = read_conversation(root, result.session_id, scope=scope)
    assert history.turns == (("Read the source.", answer),)
    assert history.answers[0].trace == (
        {"kind": "text", "text": "Opening the source."},
        {"kind": "tool", "name": "read_file", "arguments": '{"path":"sources/fixture.md"}'},
        {"kind": "text", "text": answer},
    )
    assert sum(b"ASSISTANT_BODY_987" in payload for payload in payloads) == 1


@pytest.mark.parametrize(
    ("operation", "cursor"),
    [
        ("list", '["9999",9223372036854775808]'),
        ("list", '["9999",-1]'),
        ("pending", "[0,9223372036854775808,1]"),
        ("pending", "[0,0,-1]"),
    ],
)
def test_session_cursors_reject_numbers_outside_the_storage_range(tmp_path, operation, cursor):
    from ultrakb.storage.session_store import SessionStore

    root, identity, _ = question_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    SessionStore(root, identity).create("cursor", model="gpt-4o-mini", language="en")
    with pytest.raises(ValueError, match="cursor"):
        if operation == "list":
            list_conversations(root, scope=scope, after=cursor)
        else:
            read_conversation(root, "cursor", scope=scope, pending_after=cursor)


@pytest.mark.asyncio
async def test_restored_history_rebinds_reading_identity_and_keeps_original_provenance(tmp_path):
    from ultrakb.application.answer_citations import read_answer_reference
    from ultrakb.storage.database import StorageFormatError
    from ultrakb.storage.maintenance import Maintenance

    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport([{"content": "The value is 17. See [[sources/fixture]]."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Original question.",
            new_session_id="restored-history",
            attempt_id="restored-submission",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    original = result.citations["references"][0]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    Maintenance(root, identity).backup(backup)
    recovered = Maintenance.restore(backup, restored)
    history = read_conversation(
        restored, result.session_id, scope=storage_scope(restored, recovered)
    )
    reference = history.answers[0].citations["references"][0]
    content, _, _ = read_answer_reference(restored, reference)
    assert "The value is 17." in content
    assert reference["publication_id"] == original["publication_id"]
    assert reference["digest"] == original["digest"]
    assert reference["provenance_storage_identity"] == original["storage_identity"]
    assert reference["storage_identity"] == {
        "kb_id": recovered.kb_id,
        "generation": recovered.generation,
    }
    with pytest.raises(StorageFormatError):
        read_answer_reference(restored, original)
    # Configuration is explicitly outside this development backup's coverage.
    (restored / ".ultrakb/config.yaml").write_bytes((root / ".ultrakb/config.yaml").read_bytes())
    with StreamTransport([]) as service:
        (restored / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        replayed = await continue_conversation(
            restored,
            "Original question.",
            new_session_id="restored-history",
            attempt_id="restored-submission",
            scope=storage_scope(restored, recovered),
        )
    assert replayed.status == "completed", replayed.error
    assert replayed.citations == history.answers[0].citations
    assert service.requests == []
