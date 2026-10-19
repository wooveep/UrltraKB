"""Manual retirement preserves the calculation and retrieval recovery roots."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversation_memory import ConversationMemory
from ultrakb.application.execution import ExecutionContext
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.session_store import SessionStore

pytestmark = pytest.mark.integration


def test_retirement_keeps_latest_calculation_and_older_retrieval_checkpoint(tmp_path):
    from ultrakb.application.storage_maintenance import (
        preview_root_retirement,
        retire_unused_roots,
    )

    root, identity, _ = question_knowledge(tmp_path)
    sessions = SessionStore(root, identity)
    sessions.create("retention", model="openai/gpt-4o-mini", language="en")
    sessions.append_exchange("retention", "turn-0", "Preference 0", "Original answer 0")
    responses = [{"content": json.dumps({"title": "Workspace"})}] + [
        {"content": json.dumps({"belongs_to_current": True})}
    ] * 3
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with ExecutionContext(storage_identity=identity).begin(root):
            from ultrakb.application.execution import current_execution

            memory = ConversationMemory.from_executor(
                sessions, "retention", current_execution().executor
            )
            first = memory.catch_up()
            assert (first.computed_turns, first.indexed_turns) == (1, 1)
            for ordinal in range(1, 4):
                sessions.append_exchange(
                    "retention",
                    f"turn-{ordinal}",
                    f"Preference {ordinal}",
                    f"Original answer {ordinal}",
                )
            with storage_stage(root, identity) as db, db.transaction() as connection:
                connection.execute(
                    "CREATE TRIGGER fail_retention_projection BEFORE INSERT "
                    "ON ukb_session_projections BEGIN "
                    "SELECT RAISE(ABORT, 'injected projection failure'); END"
                )
            failed = memory.catch_up()
            assert (failed.computed_turns, failed.indexed_turns) == (4, 1)

        preview = preview_root_retirement(root)
        assert (preview.candidate_checkpoints, preview.protected_checkpoints) == (2, 2)
        assert preview.candidate_reference_bytes > 0
        for _ in range(2):
            retired = retire_unused_roots(root, max_items=1)
            assert retired.status == "completed" and retired.retired_checkpoints == 1
            roots = sessions.memory_roots("retention", memory.policy_key)
            assert (roots.computed.turn_count, roots.projected.turn_count) == (4, 1)
        assert preview_root_retirement(root).candidate_checkpoints == 0
        original = sessions.load("retention")
        assert len(original.turns) == 4
        assert original.turns[-1]["messages"][-1]["content"] == "Original answer 3"
        assert sessions.restore_checkpoint("retention", memory.policy_key).turns == list(
            original.turns
        )

        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_retention_projection")
        with ExecutionContext(storage_identity=identity).begin(root):
            repaired = ConversationMemory.from_executor(
                sessions, "retention", current_execution().executor
            ).catch_up()
        assert (repaired.computed_turns, repaired.indexed_turns) == (4, 4)
        assert len(service.requests) == 4
        assert preview_root_retirement(root).candidate_checkpoints == 1
        assert retire_unused_roots(root).retired_checkpoints == 1
        assert sessions.restore_checkpoint("retention", memory.policy_key).turns == list(
            original.turns
        )


@pytest.mark.parametrize("intermediate", [b"v2", b"longer second target", None])
def test_old_sync_plans_retire_only_after_predecessor_recovery_completes(tmp_path, intermediate):
    from ultrakb.application.storage_maintenance import (
        collect_garbage,
        preview_root_retirement,
        retire_unused_roots,
    )
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"v1"}, expected=None)
    knowledge.publish(first)
    worktree = ProjectionStore(root, identity)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(
        {"index.md": intermediate} if intermediate is not None else {},
        expected=first.publication_id,
    )
    knowledge.publish(second)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_retirement_confirmation BEFORE UPDATE "
            "ON ukb_projection_applied BEGIN "
            "SELECT RAISE(ABORT, 'injected crash after replacement'); END"
        )
    assert worktree.sync().status == "pending"
    third = knowledge.prepare_revision({"index.md": b"v3"}, expected=second.publication_id)
    knowledge.publish(third)
    protected = preview_root_retirement(root)
    assert (protected.candidate_plans, protected.protected_plans) == (0, 3)
    assert retire_unused_roots(root).retired_plans == 0
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_retirement_confirmation")
    assert worktree.sync().status == "synced"
    assert (root / "wiki/index.md").read_bytes() == b"v3"
    preview = preview_root_retirement(root)
    assert (preview.candidate_plans, preview.protected_plans) == (2, 1)
    retired_plans = 0
    for _ in range(8):
        batch = preview_root_retirement(root, max_items=1)
        retired = retire_unused_roots(root, max_items=1)
        assert retired.status == "completed" and retired.released_references <= 3
        assert retired.released_reference_bytes == batch.candidate_reference_bytes
        retired_plans += retired.retired_plans
        if not retired.has_more:
            break
    assert retired_plans == 2
    assert preview_root_retirement(root).candidate_plans == 0
    assert collect_garbage(root).status == "completed"
    assert knowledge.bind(first.publication_id).read("index.md") == b"v1"
    assert knowledge.bind().read("index.md") == b"v3"
    assert worktree.sync(plan_id=second.sync_plan_id).status == "obsolete"


def test_retirement_can_run_during_model_wait_but_not_real_storage_access(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ultrakb.application.storage_maintenance import retire_unused_roots

    root, identity, _ = question_knowledge(tmp_path)
    sessions = SessionStore(root, identity)
    sessions.create("waiting", model="openai/gpt-4o-mini", language="en")
    for ordinal in range(3):
        sessions.append_exchange("waiting", f"turn-{ordinal}", f"Preference {ordinal}", "Answer")
    release, waiting = Event(), Event()

    def response(request, index):
        if index == 3:
            waiting.set()
            assert release.wait(5)
        return {
            "content": json.dumps(
                {"title": "Workspace"} if index == 0 else {"belongs_to_current": True}
            )
        }

    def catch_up():
        context = ExecutionContext(storage_identity=identity)
        with context.begin(root):
            return ConversationMemory.from_executor(
                sessions, "waiting", context.executor
            ).catch_up()

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = catch_up()
        assert first.computed_turns == 3
        sessions.append_exchange("waiting", "turn-3", "Fourth preference", "Original answer")
        with storage_stage(root, identity, read_only=True):
            assert retire_unused_roots(root).status == "busy"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(catch_up)
            try:
                assert waiting.wait(3)
                retired = retire_unused_roots(root, max_items=1)
                assert retired.status == "completed" and retired.retired_checkpoints == 1
                roots = sessions.memory_roots("waiting", first.policy_key)
                assert (roots.computed.turn_count, roots.projected.turn_count) == (3, 3)
            finally:
                release.set()
            final = pending.result(timeout=5)
        assert (final.computed_turns, final.indexed_turns) == (4, 4)
        assert len(service.requests) == 4


def test_maintenance_and_backup_do_not_require_a_published_wiki_or_model_configuration(tmp_path):
    from ultrakb.application.storage_maintenance import (
        backup_knowledge,
        collect_garbage,
        preview_collection,
        preview_root_retirement,
        restore_knowledge,
    )
    from ultrakb.storage.database import create_database

    root, archive, restored = tmp_path / "kb", tmp_path / "backup", tmp_path / "restored"
    identity = create_database(root)
    assert preview_collection(root).candidate_count == 0
    assert preview_root_retirement(root).candidate_checkpoints == 0
    assert collect_garbage(root).status == "completed"
    backup = backup_knowledge(root, archive)
    result = restore_knowledge(archive, restored)
    assert backup.identity == identity
    assert result.identity.kb_id == identity.kb_id
    assert result.identity.generation != identity.generation
    assert collect_garbage(restored).status == "completed"
    assert not (root / "wiki").exists()


def test_retired_projection_details_do_not_rewrite_task_publication_history(tmp_path):
    from ultrakb.application.knowledge_tasks import read_knowledge_execution
    from ultrakb.application.storage_maintenance import retire_unused_roots
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    executions = ExecutionStore(root, identity)
    execution = executions.begin("publish", fingerprint="fixture", max_calls=0)
    knowledge, worktree = KnowledgeStore(root, identity), ProjectionStore(root, identity)
    first = knowledge.prepare_revision({"index.md": b"v1"}, expected=None)
    knowledge.publish(first, execution_id=execution.execution_id)
    assert worktree.sync().status == "synced"
    executions.finish(execution.execution_id, state="completed")
    second = knowledge.prepare_revision({"index.md": b"v2"}, expected=first.publication_id)
    knowledge.publish(second)
    assert worktree.sync().status == "synced"
    before = read_knowledge_execution(root, execution.execution_id)
    assert before.publications[0].projection_status == "superseded"
    assert retire_unused_roots(root).retired_plans == 1
    after = read_knowledge_execution(root, execution.execution_id)
    assert after.publications[0].projection_status == "superseded"
    assert after.publications[0].projection_details == "retired"
    assert after.coverage["projection_details"] == "retired"
    assert after.task == before.task and after.execution_usage == before.execution_usage


def _two_synchronized_publications(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge, worktree = KnowledgeStore(root, identity), ProjectionStore(root, identity)
    first = knowledge.prepare_revision(
        {"index.md": b"v1", "concepts/one.md": b"one", "concepts/two.md": b"two"},
        expected=None,
    )
    knowledge.publish(first)
    assert worktree.sync().status == "synced"
    second = knowledge.prepare_revision(
        {"index.md": b"v2", "concepts/one.md": b"one", "concepts/two.md": b"two"},
        expected=first.publication_id,
    )
    knowledge.publish(second)
    assert worktree.sync().status == "synced"
    return root, identity, knowledge, worktree, first, second


@pytest.mark.parametrize("failure", ["item", "manifest"])
def test_interrupted_retirement_survives_backup_restore_and_resumes_bounded_batches(
    tmp_path, failure
):
    import sqlite3

    from ultrakb.application.storage_maintenance import (
        backup_knowledge,
        preview_root_retirement,
        restore_knowledge,
        retire_unused_roots,
    )

    root, identity, _, _, first, _ = _two_synchronized_publications(tmp_path)
    target = (
        "DELETE ON ukb_sync_items"
        if failure == "item"
        else ("UPDATE OF manifest_digest ON ukb_sync_plans")
    )
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            f"CREATE TRIGGER interrupt_retirement BEFORE {target} "
            "BEGIN SELECT RAISE(ABORT, 'injected retirement interruption'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="interruption"):
        retire_unused_roots(root)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER interrupt_retirement")
    archive, destination = tmp_path / "backup", tmp_path / "restored"
    backup_knowledge(root, archive)
    restored = restore_knowledge(archive, destination)
    assert restored.identity.generation != identity.generation
    assert preview_root_retirement(destination).candidate_plans == 1
    retired_count = 0
    for _ in range(5):
        result = retire_unused_roots(destination, max_items=1)
        retired_count += result.retired_plans
        assert result.retired_sync_items <= 1 and result.released_references <= 3
        if not result.has_more:
            break
    assert retired_count == 1
    assert preview_root_retirement(destination).candidate_plans == 0
    from ultrakb.storage.knowledge_store import KnowledgeStore

    assert (
        KnowledgeStore(destination, restored.identity).bind(first.publication_id).read("index.md")
        == b"v1"
    )


def test_retirement_rechecks_new_sync_work_and_preserves_unknown_response_ownership(tmp_path):
    from ultrakb.application.storage_maintenance import preview_root_retirement, retire_unused_roots
    from ultrakb.storage.receipt_store import ReceiptStore

    root, identity, knowledge, worktree, _, second = _two_synchronized_publications(tmp_path)
    assert preview_root_retirement(root).candidate_plans == 1
    third = knowledge.prepare_revision({"index.md": b"v3"}, expected=second.publication_id)
    knowledge.publish(third)
    assert retire_unused_roots(root).retired_plans == 0
    assert worktree.sync().status == "synced"
    assert preview_root_retirement(root).candidate_plans == 2
    pending = ReceiptStore(root, identity).pending
    pending.mkdir(parents=True)
    packet = pending / "unknown.json"
    packet.write_bytes(b"Received response with unknown owner")
    preview = preview_root_retirement(root)
    assert preview.candidate_plans == 0 and preview.protected_plans == 3
    assert preview.blocked_reason == "unresolved_pending_receipts"
    blocked = retire_unused_roots(root)
    assert blocked.status == "blocked" and blocked.released_references == 0
    assert packet.read_bytes() == b"Received response with unknown owner"
    packet.unlink()
    assert retire_unused_roots(root).retired_plans == 2
