"""Continue real SDK conversations over the selected main-store publication."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation
from ultrakb.application.views import storage_scope
from ultrakb.storage.session_store import SessionStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_continuation_keeps_originals_and_reuses_only_committed_user_intent(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport(
        [{"content": "ASSISTANT_PRIVATE_123\nOriginal answer."}, {"content": "A second answer."}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await continue_conversation(
            root,
            "USER_PREFERENCE_456\n Keep these original line breaks.",
            new_session_id="stored-conversation",
            attempt_id="submission-one",
            scope=storage_scope(root, identity),
        )
        assert first.status == "completed", (first.error, first.unfinished)
        assert first.session_id == "stored-conversation" and first.turn_count == 1
        second = await continue_conversation(
            root,
            "Explain the next step.",
            session_id=first.session_id,
            attempt_id="submission-two",
            scope=storage_scope(root, identity),
        )
        assert second.status == "completed" and second.turn_count == 2, second.error
    conversation = SessionStore(root, identity).load(first.session_id)
    assert conversation.turns[0]["messages"] == [
        {"role": "user", "content": "USER_PREFERENCE_456\n Keep these original line breaks."},
        {"role": "assistant", "content": "ASSISTANT_PRIVATE_123\nOriginal answer."},
    ]
    sent = json.dumps(service.requests[1]["messages"], ensure_ascii=False)
    assert "USER_PREFERENCE_456" in sent and "ASSISTANT_PRIVATE_123" not in sent
    assert second.context["committed_turns"] == 1
    assert second.context["status"] == "recent_only"
    assert second.model_usage["current"]["actual_sends"] == 1
    assert not (root / ".ultrakb/chats").exists()


@pytest.mark.asyncio
async def test_completed_submission_replays_its_original_answer_without_another_send(tmp_path):
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport([{"content": "See [[sources/fixture]]."}] * 3) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        arguments = {"new_session_id": "replayed", "attempt_id": "same-submission"}
        first = await continue_conversation(
            root, "Read the original value.", scope=storage_scope(root, identity), **arguments
        )
        assert first.status == "completed", first.error
        knowledge = KnowledgeStore(root, identity)
        knowledge.publish(
            knowledge.prepare_changes(knowledge.bind(), {"sources/fixture.md": b"New value 99."})
        )
        repeated = await continue_conversation(
            root, "Read the original value.", scope=storage_scope(root, identity), **arguments
        )
        assert repeated.status == "completed" and repeated.answer == first.answer, repeated.error
        assert repeated.turn_count == 1 and repeated.citations == first.citations
        rejected = await continue_conversation(
            root, "A different submission.", scope=storage_scope(root, identity), **arguments
        )
    assert rejected.status == "failed"
    assert len(service.requests) == 1
    assert len(SessionStore(root, identity).turns("replayed")) == 1
    assert repeated.model_usage["current"]["actual_sends"] == 0


@pytest.mark.asyncio
async def test_replay_does_not_hide_a_submission_order_mismatch(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport([{"content": "One committed answer."}] * 2) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        arguments = {"new_session_id": "ordered", "attempt_id": "stable-message"}
        first = await continue_conversation(
            root,
            "A stable message.",
            submission_order=1,
            scope=storage_scope(root, identity),
            **arguments,
        )
        assert first.status == "completed", first.error
        rejected = await continue_conversation(
            root,
            "A stable message.",
            submission_order=2,
            scope=storage_scope(root, identity),
            **arguments,
        )
    assert rejected.status == "failed" and rejected.turn_count == 1
    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_persisted_stop_rejects_a_late_turn_before_the_memory_flag_changes(
    tmp_path, monkeypatch
):
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, _ = question_knowledge(tmp_path)
    append = SessionStore.append_exchange

    def stopped_before_acceptance(store, *args, **kwargs):
        assert ExecutionStore(root, identity).finish(kwargs["execution_id"], state="cancelled")
        return append(store, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "append_exchange", stopped_before_acceptance)
    with StreamTransport([{"content": "A late answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Read the value.",
            new_session_id="durable-stop",
            scope=storage_scope(root, identity),
        )
    assert result.status == "stopped"
    assert SessionStore(root, identity).turns("durable-stop") == []
    assert result.model_usage["current"]["actual_sends"] == 1


@pytest.mark.asyncio
async def test_continuation_model_wait_allows_gc_settings_and_publication_changes(tmp_path):
    import asyncio
    from threading import Event

    from ultrakb.application.execution_configuration import patch_storage_config
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.locks import file_write_lock
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.maintenance import Maintenance

    root, identity, first = question_knowledge(tmp_path)
    fixed = storage_scope(root, identity)
    gate = Event()
    with storage_stage(root, identity) as db:
        orphan = put_blob(db, b"unrelated conversation orphan")
    responses = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-original",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/fixture.md"}'},
                }
            ]
        },
        {"content": "The original value is 17. See [[sources/fixture]]."},
    ]
    with StreamTransport(responses, gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(
            continue_conversation(root, "Read the value.", new_session_id="concurrent", scope=fixed)
        )
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            with file_write_lock(root / ".ultrakb/ingest.lock"):
                collected = await asyncio.to_thread(Maintenance(root, identity).collect)
                assert collected.status == "completed" and collected.deleted_count == 1
                await asyncio.to_thread(
                    patch_storage_config,
                    root,
                    identity,
                    KbConfigPatchRequest(kb=str(root), config={"language": "zh"}),
                )
                knowledge = KnowledgeStore(root, identity)
                latest = knowledge.publish(
                    knowledge.prepare_changes(
                        knowledge.bind(), {"sources/fixture.md": b"New value 99."}
                    )
                )
        finally:
            gate.set()
        result = await task
    assert result.status == "completed" and result.turn_count == 1, result.error
    assert KnowledgeStore(root, identity).bind().publication_id == latest.publication_id
    assert first.publication_id == result.citations["references"][0]["publication_id"]
    messages = service.requests[1]["messages"]
    assert any(m["role"] == "tool" and "The value is 17." in str(m["content"]) for m in messages)
    assert "Answer in en language" in json.dumps(messages)
    assert result.model_usage["current"]["actual_sends"] == 2
    with storage_stage(root, identity, read_only=True) as db:
        assert (
            db.connection.execute(
                "SELECT 1 FROM ukb_blobs WHERE digest = ?", (orphan.digest,)
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_cancelled_conversation_keeps_paid_receipts_and_unfinished_submission(tmp_path):
    from threading import Event

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root, identity, _ = question_knowledge(tmp_path)
    stopped, body_gate = Event(), Event()

    def stop_after_delta(event):
        if event.get("event") == "delta":
            stopped.set()
            body_gate.set()

    context = ExecutionContext(
        storage_identity=identity, cancelled=stopped.is_set, on_event=stop_after_delta
    )
    with StreamTransport([{"content": "Partial answer"}], body_gate=body_gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Original pending user message.",
            new_session_id="stopped",
            attempt_id="pending-submission",
            submission_order=7,
            scope=storage_scope(root, identity),
            context=context,
        )
    assert result.status == "stopped" and result.turn_count == 0
    assert SessionStore(root, identity).turns("stopped") == []
    execution_id = context.storage_execution.execution_id
    [request] = UsageStore(root, identity).requests(execution_id=execution_id).records
    assert request.business_state == "cancelled"
    assert ReceiptStore(root, identity).read(request.request_attempt_id) == service.bodies[0]
    assert result.model_usage["current"]["input_total"] == 120
    assert ExecutionStore(root, identity).status(execution_id) == "cancelled"
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute(
            "SELECT submission_order FROM ukb_conversation_submissions WHERE session_id = 'stopped'"
        ).fetchone() == (7,)
        assert db.connection.execute(
            "SELECT state FROM ukb_conversation_runs WHERE execution_id = ?", (execution_id,)
        ).fetchone() == ("stopped",)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["before_commit", "after_commit", "lost_reply"])
async def test_conversation_turn_commit_survives_stop_and_lost_acknowledgement(
    tmp_path, monkeypatch, window
):
    import asyncio
    from threading import Event

    root, identity, _ = question_knowledge(tmp_path)
    entered, release = Event(), Event()
    append = SessionStore.append_exchange

    def gated_append(store, *args, **kwargs):
        if window != "before_commit":
            ordinal = append(store, *args, **kwargs)
        if window == "lost_reply":
            raise OSError("Injected lost acknowledgement after the durable turn commit")
        entered.set()
        assert release.wait(3), "The parent application could not release its worker"
        return append(store, *args, **kwargs) if window == "before_commit" else ordinal

    monkeypatch.setattr(SessionStore, "append_exchange", gated_append)
    with StreamTransport([{"content": "A complete original answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(
            continue_conversation(
                root,
                "Read the value.",
                new_session_id="commit-window",
                attempt_id="commit-once",
                scope=storage_scope(root, identity),
            )
        )
        try:
            if window != "lost_reply":
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "The application abandoned its active worker"
        finally:
            release.set()
        result = await task
    assert result.model_usage["current"]["actual_sends"] == len(service.requests) == 1
    assert result.status == ("stopped" if window == "before_commit" else "completed"), result.error
    turns = SessionStore(root, identity).turns("commit-window")
    assert len(turns) == (0 if window == "before_commit" else 1)


@pytest.mark.asyncio
async def test_failed_turn_transaction_keeps_the_response_and_commits_no_half_turn(tmp_path):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root, identity, _ = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_turn_result BEFORE INSERT ON ukb_turn_results "
            "BEGIN SELECT RAISE(ABORT, 'injected turn result failure'); END"
        )
    with StreamTransport([{"content": "A fully paid answer."}] * 2) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Read the value.",
            new_session_id="failed-commit",
            attempt_id="accepted-answer",
            scope=storage_scope(root, identity),
            context=context,
        )
        assert SessionStore(root, identity).turns("failed-commit") == []
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_turn_result")
        recovered = await continue_conversation(
            root,
            "Read the value.",
            new_session_id="failed-commit",
            attempt_id="accepted-answer",
            scope=storage_scope(root, identity),
        )
    assert result.status == "failed" and result.turn_count == 0
    assert recovered.status == "completed" and recovered.answer == "A fully paid answer.", (
        recovered.error
    )
    assert recovered.turn_count == 1 and len(service.requests) == 1
    assert recovered.model_usage["current"]["actual_sends"] == 0
    [request] = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert ReceiptStore(root, identity).read(request.request_attempt_id) == service.bodies[0]
    assert result.model_usage["current"]["actual_sends"] == 1


@pytest.mark.asyncio
async def test_continuation_builds_one_real_recall_tree_and_returns_only_selected_users(tmp_path):
    import yaml

    from ultrakb.config import save_config

    root, identity, _ = question_knowledge(tmp_path)
    path = root / ".ultrakb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["model_policy"]["max_calls"] = 32
    save_config(path, config)
    store = SessionStore(root, identity)
    store.create("recall", model=config["model"], language="en")
    for ordinal in range(7):
        store.append_exchange(
            "recall",
            f"turn-{ordinal}",
            f"USER_MARKER_{ordinal} " + "Preference for quiet workplaces. " * 300,
            f"ASSISTANT_DISPLAY_ONLY_{ordinal}",
        )

    def provider(request, index):
        if request.get("stream"):
            return {"content": "I will use your earlier preference."}
        text = json.dumps(request["messages"])
        if "belongs_to_current" in text:
            return {"content": '{"belongs_to_current":true}'}
        if "Name the subject" in text:
            return {"content": '{"title":"Workspace preferences"}'}
        if "Summarize" in text:
            return {"content": '{"summary":"User workplace preferences"}'}
        rank = next(
            tool["function"] for tool in request["tools"] if tool["function"]["name"] == "rank"
        )
        choices = rank["parameters"]["properties"]["ranked_ids"]["items"]["enum"]
        return {
            "tool_calls": [
                {
                    "id": f"rank-{index}",
                    "type": "function",
                    "function": {
                        "name": "rank",
                        "arguments": json.dumps({"ranked_ids": choices[:6], "done": False}),
                    },
                }
            ]
        }

    with StreamTransport(provider) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "What earlier workplace preference should guide us?",
            session_id="recall",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", (result.error, result.context)
    index = result.context["index"]
    assert 0 < index["indexed_turns"] <= 7, result.context
    assert index["computed_turns"] == index["indexed_turns"], result.context
    assert index["pending_turns"] == 7 - index["indexed_turns"], result.context
    if index["pending_turns"]:
        assert result.context["status"] == "limited"
    assert result.context["recall"]["status"] == "completed", result.context
    assert "turn-0" in result.context["recall"]["turn_ids"], result.context
    assert "USER_MARKER_0" in json.dumps(service.requests[-1]["messages"])
    assert "ASSISTANT_DISPLAY_ONLY" not in json.dumps(service.requests)
    from ultrakb.storage.coordination import storage_stage

    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute(
            "SELECT count(*) FROM ukb_tree_builds WHERE protocol = 'chatindex' AND state = 'ready'"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_long_conversation_without_older_candidates_sends_only_the_answer(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("recent-only", model="openai/gpt-4o-mini", language="en")
    for ordinal in range(6):
        store.append_exchange(
            "recent-only",
            f"turn-{ordinal}",
            f"USER_RECENT_{ordinal} " + "Preference for quiet workplaces. " * 300,
            "ASSISTANT_ONLY_DISPLAY",
        )
    with StreamTransport([{"content": "A complete answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root,
            "Continue from our preferences.",
            session_id="recent-only",
            scope=storage_scope(root, identity),
        )
    assert result.status == "completed", result.error
    assert result.context["committed_user_tokens"] >= 8000
    assert result.context["status"] == "recent_only"
    assert result.context["recall_reason"] == "no_recall_candidates"
    assert len(service.requests) == 1 and service.requests[0]["stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_while_queued", [False, True])
async def test_queued_conversation_can_stop_and_cannot_revive_a_deleted_session(
    tmp_path, delete_while_queued
):
    import asyncio
    from threading import Event

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import session_lock
    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    SessionStore(root, identity).create("queued", model="openai/gpt-4o-mini", language="en")
    waiting = Event()
    context = ExecutionContext(
        storage_identity=identity,
        on_event=lambda event: waiting.set() if event.get("stage") == "waiting" else None,
    )
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with session_lock(root, "queued", database_identity=identity):
            task = asyncio.create_task(
                continue_conversation(
                    root,
                    "A queued question.",
                    session_id="queued",
                    context=context,
                    scope=storage_scope(root, identity),
                )
            )
            assert await asyncio.to_thread(waiting.wait, 3)
            if delete_while_queued:
                with storage_stage(root, identity) as db, db.transaction() as connection:
                    connection.execute(
                        "UPDATE ukb_conversations SET deleted = 1 WHERE session_id = 'queued'"
                    )
            else:
                task.cancel()
                # Stop must finish while another real owner still holds the lock.
                result = await asyncio.wait_for(asyncio.shield(task), 1)
        result = await task
    assert result.status == ("failed" if delete_while_queued else "stopped"), result.error
    assert result.turn_count == 0 and not service.requests
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute("SELECT count(*) FROM ukb_turns").fetchone() == (0,)


def test_a_submission_cannot_claim_completion_without_its_atomic_turn_result(tmp_path):
    import sqlite3

    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, publication = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("no-false-success", model="openai/gpt-4o-mini", language="en")
    execution = ExecutionStore(root, identity).begin(
        "conversation", fingerprint="fixed", max_calls=4
    )
    store.start_submission(
        "no-false-success",
        "submitted",
        "Original question.",
        execution_id=execution.execution_id,
        publication_id=publication.publication_id,
    )
    with pytest.raises(ValueError):
        store.finish_submission(execution.execution_id, "completed")
    with storage_stage(root, identity) as db:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
            connection.execute("UPDATE ukb_conversation_runs SET state = 'completed', ordinal = 0")
        assert db.connection.execute(
            "SELECT state, ordinal FROM ukb_conversation_runs"
        ).fetchone() == ("started", None)


@pytest.mark.parametrize("command", ["append", "finish", "start"])
def test_restored_sessions_reject_original_execution_tokens(tmp_path, command):
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.maintenance import Maintenance

    root, identity, publication = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("original", model="openai/gpt-4o-mini", language="en")
    execution = ExecutionStore(root, identity).begin(
        "conversation", fingerprint="fixed", max_calls=4
    )
    store.start_submission(
        "original",
        "submitted",
        "Original question.",
        execution_id=execution.execution_id,
        publication_id=publication.publication_id,
    )
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    Maintenance(root, identity).backup(backup)
    restored_identity = Maintenance.restore(backup, restored)
    restored_store = SessionStore(restored, restored_identity)
    with pytest.raises(ValueError):
        if command == "append":
            restored_store.append_exchange(
                "original",
                "late-turn",
                "Original question.",
                "Late original-process answer.",
                execution_id=execution.execution_id,
                attempt_id="submitted",
                metadata={},
            )
        elif command == "finish":
            restored_store.finish_submission(execution.execution_id, "failed")
        else:
            restored_store.start_submission(
                "original",
                "another",
                "Another question.",
                execution_id=execution.execution_id,
                publication_id=publication.publication_id,
            )
    assert restored_store.turns("original") == []


@pytest.mark.asyncio
async def test_known_unsent_terminal_attempt_can_retry_the_same_submission(tmp_path, monkeypatch):
    import litellm

    root, identity, _ = question_knowledge(tmp_path)

    async def fail_before_transport(*args, **kwargs):
        raise ValueError("Injected provider failure before opening HTTP")

    arguments = {"new_session_id": "known-unsent", "attempt_id": "same-question"}
    with StreamTransport([{"content": "The first actually sent answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as provider:
            provider.setattr(litellm, "acompletion", fail_before_transport)
            first = await continue_conversation(
                root,
                "A stable question.",
                scope=storage_scope(root, identity),
                **arguments,
            )
        assert first.status == "failed" and first.model_usage["current"]["actual_sends"] == 0
        assert first.model_usage["current"]["unsent_attempts"] == 1
        second = await continue_conversation(
            root,
            "A stable question.",
            scope=storage_scope(root, identity),
            **arguments,
        )
    assert second.status == "completed", second.error
    assert len(service.requests) == second.model_usage["current"]["actual_sends"] == 1


@pytest.mark.asyncio
async def test_uncertain_paid_answer_acceptance_blocks_a_second_model_send(tmp_path):
    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_answer_acceptance BEFORE INSERT ON ukb_conversation_answers "
            "BEGIN SELECT RAISE(ABORT, 'injected answer acceptance failure'); END"
        )
    arguments = {"new_session_id": "unconfirmed-paid", "attempt_id": "paid-once"}
    with StreamTransport([{"content": "A paid answer awaiting recovery."}] * 2) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await continue_conversation(
            root,
            "A stable question.",
            scope=storage_scope(root, identity),
            **arguments,
        )
        assert first.status == "failed" and first.model_usage["current"]["actual_sends"] == 1
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_answer_acceptance")
        resumed = await continue_conversation(
            root,
            "A stable question.",
            scope=storage_scope(root, identity),
            **arguments,
        )
    assert resumed.status == "blocked" and resumed.turn_count == 0
    assert len(service.requests) == 1 and resumed.model_usage["current"]["actual_sends"] == 0
    assert SessionStore(root, identity).turns("unconfirmed-paid") == []
