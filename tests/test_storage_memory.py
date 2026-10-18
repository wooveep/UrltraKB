"""The shared application memory use case uses real CTree and ConDB stores."""

import pytest
from test_storage_sessions import TopicResponses


def test_catch_up_commits_each_prefix_but_builds_only_the_final_tree(tmp_path, monkeypatch):
    from contextdb import ConDB

    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.storage.database import create_database, database_path
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = SessionStore(root, identity)
    store.create("one", model="test/model", language="en")
    for ordinal in range(3):
        store.append_exchange(
            "one",
            f"turn-{ordinal}",
            f"Preference {ordinal}",
            "DISPLAY_ONLY",
            system="SYSTEM_DISPLAY_ONLY",
        )

    class TopicProvider(TopicResponses):
        def complete(self, messages, **options):
            assert Maintenance(root, identity).collect().status != "busy"
            return super().complete(messages, **options)

    provider, prepared, prefixes = TopicProvider(), [], []
    original_prepare, original_save = ConDB.prepare, store.save_checkpoint

    def observed_prepare(data, **options):
        if options["format"] == "chatindex":
            prepared.append(data["basis"]["turn_count"])
        return original_prepare(data, **options)

    def observed_save(*args, **options):
        checkpoint = original_save(*args, **options)
        prefixes.append(checkpoint.turn_count)
        return checkpoint

    monkeypatch.setattr(ConDB, "prepare", observed_prepare)
    monkeypatch.setattr(store, "save_checkpoint", observed_save)
    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=provider,
    )
    state = memory.catch_up()
    assert (state.computed_turns, state.indexed_turns, state.pending_turns) == (3, 3, 0)
    assert state.tree_id is not None
    assert prefixes == [1, 2, 3]
    assert prepared == [3]
    assert len(provider.requests) == 3
    path = database_path(root)
    wal = path.with_name(path.name + "-wal")
    before = wal.read_bytes()
    assert memory.catch_up() == state
    assert wal.read_bytes() == before
    assert prefixes == [1, 2, 3] and prepared == [3]
    assert len(provider.requests) == 3


def test_cancelled_memory_does_not_wait_for_another_rounds_session_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.locks import LockCancelled, session_lock
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store = SessionStore(root, create_database(root))
    store.create("one", model="test/model", language="en")

    def stopped():
        raise LockCancelled("cancelled before acquiring the session")

    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=TopicResponses(),
        check_cancelled=stopped,
    )
    with ThreadPoolExecutor(max_workers=1) as worker:
        with session_lock(root, "one"):
            pending = worker.submit(memory.catch_up)
            with pytest.raises(LockCancelled):
                pending.result(timeout=0.5)


def test_projection_commit_failure_keeps_old_coverage_and_reuses_new_computation(tmp_path):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store, provider = SessionStore(root, identity), TopicResponses()
    store.create("one", model="test/model", language="en")
    store.append_exchange("one", "first", "First preference", "DISPLAY_ONLY")
    memory = ConversationMemory(
        store, "one", policy_key="policy-one", model="test/model", llm=provider
    )
    first = memory.catch_up()
    store.append_exchange("one", "second", "Second preference", "DISPLAY_ONLY")
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER injected_projection_failure BEFORE INSERT ON ukb_session_projections "
            "BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END"
        )
    failed = memory.catch_up()
    assert (failed.computed_turns, failed.indexed_turns, failed.pending_turns) == (2, 1, 1)
    assert failed.tree_id == first.tree_id and failed.prefix_digest == first.prefix_digest
    assert len(provider.requests) == 2
    assert Maintenance(root, identity).collect().status != "busy"
    assert store.restore_checkpoint("one", "policy-one").turns[-1]["turn_id"] == "second"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER injected_projection_failure")
    repaired = memory.catch_up()
    assert (repaired.computed_turns, repaired.indexed_turns, repaired.pending_turns) == (2, 2, 0)
    assert repaired.tree_id != first.tree_id
    assert len(provider.requests) == 2


def test_failed_last_checkpoint_projects_only_the_last_durable_prefix(tmp_path):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    store = SessionStore(root, identity)
    store.create("one", model="test/model", language="en")
    for ordinal in range(3):
        store.append_exchange("one", f"turn-{ordinal}", f"Preference {ordinal}", "DISPLAY_ONLY")
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER injected_checkpoint_failure "
            "BEFORE INSERT ON ukb_conversation_checkpoints "
            "WHEN NEW.turn_count = 3 BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
        )
    provider = TopicResponses()
    state = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=provider,
    ).catch_up()
    assert (state.computed_turns, state.indexed_turns, state.pending_turns) == (2, 2, 1)
    assert store.restore_checkpoint("one", "policy-one").turns[-1]["turn_id"] == "turn-1"
    assert len(provider.requests) == 3


def test_cancelled_analysis_does_not_accept_a_late_checkpoint_or_build_a_tree(tmp_path):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store = SessionStore(root, create_database(root))
    store.create("one", model="test/model", language="en")
    store.append_exchange("one", "first", "Preference", "DISPLAY_ONLY")
    stopped = False

    def check():
        if stopped:
            raise LockCancelled("stopped")

    class Provider(TopicResponses):
        def complete(self, messages, **options):
            nonlocal stopped
            response = super().complete(messages, **options)
            stopped = True
            return response

    provider = Provider()
    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=provider,
        check_cancelled=check,
    )
    with pytest.raises(LockCancelled):
        memory.catch_up()
    state = memory.state()
    assert (state.computed_turns, state.indexed_turns, state.pending_turns) == (0, 0, 1)
    assert state.tree_id is None and len(provider.requests) == 1


def test_preparation_skips_indexing_without_recall_budget_or_old_candidates(tmp_path):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store, provider = SessionStore(root, create_database(root)), TopicResponses()
    store.create("one", model="test/model", language="en")
    store.append_exchange("one", "first", "Preference", "DISPLAY_ONLY")
    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=provider,
    )
    zero = memory.prepare(committed_user_tokens=9000, recall_budget=0, recent_turn_ids=set())
    assert (zero.status, zero.reason) == ("recent_only", "recall_budget_zero")
    recent = memory.prepare(
        committed_user_tokens=9000,
        recall_budget=2000,
        recent_turn_ids={"first"},
    )
    assert (recent.status, recent.reason) == ("recent_only", "no_recall_candidates")
    assert store.memory_roots("one", "policy-one").computed is None
    assert provider.requests == []


def test_stop_during_checkpoint_blob_sync_cannot_accept_the_checkpoint(tmp_path, monkeypatch):
    import ultrakb.storage.session_store as checkpoints
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store = SessionStore(root, create_database(root))
    store.create("one", model="test/model", language="en")
    store.append_exchange("one", "first", "Preference", "DISPLAY_ONLY")
    stopped, original = False, checkpoints.put_blob

    def sync_then_stop(*args, **options):
        nonlocal stopped
        blob = original(*args, **options)
        stopped = True
        return blob

    def check():
        if stopped:
            raise LockCancelled("stopped during checkpoint sync")

    monkeypatch.setattr(checkpoints, "put_blob", sync_then_stop)
    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=TopicResponses(),
        check_cancelled=check,
    )
    with pytest.raises(LockCancelled):
        memory.catch_up()
    assert memory.state().computed_turns == 0


def test_stage_budget_limit_projects_one_accepted_prefix_and_next_call_resumes(tmp_path):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.llm_execution import StageBudgetExceeded
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store = SessionStore(root, create_database(root))
    store.create("one", model="test/model", language="en")
    for ordinal in range(3):
        store.append_exchange("one", f"turn-{ordinal}", f"Preference {ordinal}", "DISPLAY_ONLY")

    class Provider(TopicResponses):
        limited = True

        def complete(self, messages, **options):
            if self.limited and len(self.requests) == 2:
                raise StageBudgetExceeded("stage limit")
            return super().complete(messages, **options)

    provider = Provider()
    memory = ConversationMemory(
        store,
        "one",
        policy_key="policy-one",
        model="test/model",
        llm=provider,
    )
    partial = memory.catch_up()
    assert (partial.computed_turns, partial.indexed_turns, partial.pending_turns) == (2, 2, 1)
    assert any("index_lag" in reason for reason in partial.diagnostics)
    provider.limited = False
    complete = memory.catch_up()
    assert (complete.computed_turns, complete.indexed_turns, complete.pending_turns) == (3, 3, 0)
    assert len(provider.requests) == 3


@pytest.mark.parametrize("policy_size", [70000, 64460])
def test_memory_rejects_unpersistable_policy_before_any_model_request(tmp_path, policy_size):
    from ultrakb.application.conversation_memory import ConversationMemory
    from ultrakb.storage.database import create_database
    from ultrakb.storage.session_store import SessionStore

    root = tmp_path / "kb"
    store, provider = SessionStore(root, create_database(root)), TopicResponses()
    store.create("one", model="test/model", language="en")
    with pytest.raises(ValueError, match="metadata"):
        ConversationMemory(store, "one", policy_key="p" * policy_size, model="m", llm=provider)
    assert provider.requests == []
