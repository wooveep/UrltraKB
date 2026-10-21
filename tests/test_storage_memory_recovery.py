"""Conversation analysis replays durable SDK responses after acceptance failures."""

import json

import pytest
from stream_transport import StreamTransport

from ultrakb.application.conversation_memory import ConversationMemory
from ultrakb.application.execution import ExecutionContext
from ultrakb.config import save_config
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import create_database
from ultrakb.storage.receipt_store import ReceiptStore
from ultrakb.storage.session_store import SessionStore


@pytest.fixture
def memory_store(tmp_path):
    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 10, "retries": 0},
            "model_capacity": {
                "max_input_tokens": 65536,
                "tokenizer_model": "gpt-4o-mini",
            },
        },
    )
    store = SessionStore(root, identity)
    store.create("session", model="openai/gpt-4o-mini", language="en")
    store.append_exchange("session", "turn-1", "Remember a quiet workspace.", "Display only")
    return store


@pytest.mark.parametrize("new_execution", [False, True])
@pytest.mark.parametrize("repair", [False, True])
def test_received_analysis_survives_checkpoint_failure(memory_store, new_execution, repair):
    store = memory_store
    with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER injected_checkpoint_failure "
            "BEFORE INSERT ON ukb_conversation_checkpoints "
            "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
        )
    responses = ([{"content": "{}"}] if repair else []) + [{"content": '{"title":"Preferences"}'}]
    sends = len(responses)
    with StreamTransport(responses * 2) as service:
        (store.root / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        context = ExecutionContext(storage_identity=store.identity)
        with context.begin(store.root):
            memory = ConversationMemory.from_executor(store, "session", context.executor)
            failed = memory.catch_up()
            assert failed.computed_turns == failed.indexed_turns == 0
            assert any("index_lag" in reason for reason in failed.diagnostics)
            assert len(service.requests) == sends
            with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM ukb_response_receipts"
                ).fetchone() == (sends,)
                connection.execute("DROP TRIGGER injected_checkpoint_failure")
            if not new_execution:
                repaired = ConversationMemory.from_executor(
                    store, "session", context.executor
                ).catch_up()
        if new_execution:
            # A later user operation has a new budget and reconstructs all readers.
            context = ExecutionContext(storage_identity=store.identity)
            reopened = SessionStore(store.root, store.identity)
            with context.begin(store.root):
                repaired = ConversationMemory.from_executor(
                    reopened, "session", context.executor
                ).catch_up()
        assert (repaired.computed_turns, repaired.indexed_turns, repaired.pending_turns) == (
            1,
            1,
            0,
        )
        assert len(service.requests) == sends, "Recovery resent an already received topic response"
        assert "Display only" not in json.dumps(service.requests)
        with storage_stage(store.root, store.identity, read_only=True) as db:
            assert db.connection.execute("SELECT sum(used) FROM ukb_model_budgets").fetchone() == (
                sends,
            )


def test_pending_analysis_receipt_blocks_resends_until_repaired(memory_store):
    store = memory_store
    with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER injected_receipt_failure BEFORE INSERT ON ukb_response_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
        )
    with StreamTransport([{"content": '{"title":"Preferences"}'}] * 2) as service:
        (store.root / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        context = ExecutionContext(storage_identity=store.identity)
        with pytest.raises(RecoveryRequired), context.begin(store.root):
            failed = ConversationMemory.from_executor(store, "session", context.executor).catch_up()
            assert failed.computed_turns == 0
        waiting = ExecutionContext(storage_identity=store.identity)
        with waiting.begin(store.root):
            blocked = ConversationMemory.from_executor(
                store, "session", waiting.executor
            ).catch_up()
            assert blocked.computed_turns == 0
            assert "index_lag:RecoveryRequired" in blocked.diagnostics
            assert len(service.requests) == 1
        with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER injected_receipt_failure")
        assert ReceiptStore(store.root, store.identity).repair().repaired >= 1
        later = ExecutionContext(storage_identity=store.identity)
        with later.begin(store.root):
            repaired = ConversationMemory.from_executor(store, "session", later.executor).catch_up()
        assert (repaired.computed_turns, repaired.indexed_turns) == (1, 1)
        assert len(service.requests) == 1


@pytest.mark.parametrize("changed", ["session", "model", "input"])
def test_analysis_replay_is_bound_to_session_policy_and_exact_input(memory_store, changed):
    store = memory_store

    def response(request, ordinal):
        text = json.dumps(request["messages"])
        return {
            "content": '{"belongs_to_current":true}'
            if "Choose whether the new exchange" in text
            else '{"title":"Preferences"}'
        }

    with StreamTransport(response) as service:
        (store.root / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        context = ExecutionContext(storage_identity=store.identity)
        with context.begin(store.root):
            first = ConversationMemory.from_executor(store, "session", context.executor).catch_up()
        assert first.computed_turns == 1 and len(service.requests) == 1
        session_id = "session"
        if changed == "session":
            session_id = "another-session"
            store.create(session_id, model="openai/gpt-4o-mini", language="en")
            store.append_exchange(
                session_id, "turn-1", "Remember a quiet workspace.", "Display only"
            )
        elif changed == "model":
            from ultrakb.config import load_config

            config_path = store.root / ".ultrakb/config.yaml"
            save_config(config_path, {**load_config(config_path), "model": "openai/gpt-4o"})
        else:
            store.append_exchange(
                "session", "turn-2", "Remember a quiet workspace.", "Display only"
            )
        later = ExecutionContext(storage_identity=store.identity)
        with later.begin(store.root):
            current = ConversationMemory.from_executor(store, session_id, later.executor).catch_up()
        assert current.computed_turns == (2 if changed == "input" else 1)
        assert len(service.requests) == 2


def test_restored_generation_replays_retained_analysis(memory_store, tmp_path):
    from ultrakb.storage.maintenance import Maintenance

    store = memory_store
    with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER injected_checkpoint_failure "
            "BEFORE INSERT ON ukb_conversation_checkpoints "
            "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
        )
    with StreamTransport([{"content": '{"title":"Preferences"}'}] * 2) as service:
        (store.root / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        context = ExecutionContext(storage_identity=store.identity)
        with context.begin(store.root):
            failed = ConversationMemory.from_executor(store, "session", context.executor).catch_up()
            assert failed.computed_turns == 0
        with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER injected_checkpoint_failure")
        backup, destination = tmp_path / "backup", tmp_path / "restored"
        Maintenance(store.root, store.identity).backup(backup)
        restored_identity = Maintenance.restore(backup, destination)
        assert restored_identity.generation != store.identity.generation
        restored = SessionStore(destination, restored_identity)
        resumed = ExecutionContext(storage_identity=restored_identity)
        with resumed.begin(destination):
            result = ConversationMemory.from_executor(
                restored, "session", resumed.executor
            ).catch_up()
        assert (result.computed_turns, result.indexed_turns) == (1, 1)
        assert len(service.requests) == 1


def test_sdk_cached_analysis_remains_replayable_after_cache_eviction(memory_store, monkeypatch):
    import litellm
    from litellm.caching.caching import Cache

    store = memory_store
    for name in ("input_callback", "success_callback", "_async_success_callback"):
        monkeypatch.setattr(litellm, name, list(getattr(litellm, name)))
    monkeypatch.setattr(litellm, "cache", Cache(type="local"))
    store.create("cached-session", model="openai/gpt-4o-mini", language="en")
    store.append_exchange("cached-session", "turn-1", "Remember a quiet workspace.", "Display only")
    with StreamTransport([{"content": '{"title":"Preferences"}'}] * 2) as service:
        (store.root / ".env").write_text(
            f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        warm = ExecutionContext(storage_identity=store.identity)
        with warm.begin(store.root):
            assert (
                ConversationMemory.from_executor(store, "session", warm.executor)
                .catch_up()
                .computed_turns
                == 1
            )
        with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER injected_checkpoint_failure "
                "BEFORE INSERT ON ukb_conversation_checkpoints "
                "WHEN NEW.session_id = 'cached-session' "
                "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
            )
        context = ExecutionContext(storage_identity=store.identity)
        with context.begin(store.root):
            failed = ConversationMemory.from_executor(
                store, "cached-session", context.executor
            ).catch_up()
            assert failed.computed_turns == 0
            assert len(service.requests) == 1
            with storage_stage(store.root, store.identity) as db, db.transaction() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM ukb_usage_requests "
                    "WHERE execution_id = ? AND "
                    "json_extract(payload_json, '$.application_cache_hit') = 1",
                    (context.storage_execution.execution_id,),
                ).fetchone() == (1,)
                connection.execute("DROP TRIGGER injected_checkpoint_failure")
            monkeypatch.setattr(litellm, "cache", None)
            repaired = ConversationMemory.from_executor(
                store, "cached-session", context.executor
            ).catch_up()
        assert repaired.computed_turns == 1
        assert len(service.requests) == 1
