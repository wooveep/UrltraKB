"""Only unused compiler diagnostics can age out; paid responses and recovery stay."""

import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.receipt_store import ReceiptStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@asynccontextmanager
async def compiler_case(tmp_path, *, retain_summary=False):
    root, identity, target = compile_input(tmp_path)
    context = ExecutionContext()
    if retain_summary:
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER pause_accepted_summary BEFORE INSERT ON ukb_compile_steps "
                "WHEN NEW.step_key = 'short-summary' "
                "BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END"
            )
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"unexpected": True})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            scope=storage_scope(root, identity),
            context=context,
        )
        if retain_summary:
            assert result.status == "blocked" and len(service.requests) == 1
            with storage_stage(root, identity) as db, db.transaction() as connection:
                connection.execute("DROP TRIGGER pause_accepted_summary")
            result = await recompile_document(
                root,
                target.source_id,
                unit_id=target.unit_id,
                scope=storage_scope(root, identity),
                context=context,
            )
        assert result.status == "compiled", result.message
        assert len(service.requests) == 3
        yield root, identity, target, context, service


@pytest_asyncio.fixture
async def repaired_compile(tmp_path):
    async with compiler_case(tmp_path) as result:
        yield result


async def test_expired_unused_diagnostics_archive_without_losing_paid_recovery(repaired_compile):
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention

    root, identity, target, context, service = repaired_compile
    task_id = context.storage_execution.task_id
    before = read_knowledge_task(root, task_id, limit=128)
    receipts = ReceiptStore(root, identity)
    bodies = [receipts.read(request.request_attempt_id) for request in before.requests]
    retention = DiagnosticRetention(root, identity)
    recent = retention.preview()
    assert recent.retention_days == 30 and recent.payload_target_bytes == 512 * 1024**2
    assert recent.candidate_count == 0
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("UPDATE ukb_compile_responses SET created_at = '2020-01-01T00:00:00Z'")
    preview = retention.preview(max_items=1)
    assert preview.candidate_count > 0 and preview.protected_count > 0
    archived = retention.archive(max_items=1)
    assert archived.archived_count == 1 and archived.released_reference_bytes > 0
    after = read_knowledge_task(root, task_id, limit=128)
    assert after.task == before.task and after.requests == before.requests
    assert after.coverage["compiler_diagnostics"] == "partial"
    assert [receipts.read(request.request_attempt_id) for request in after.requests] == bodies
    recovered = await recompile_document(
        root,
        target.source_id,
        unit_id=target.unit_id,
        scope=storage_scope(root, identity),
        context=context,
    )
    assert recovered.status == "compiled" and len(service.requests) == 3


@pytest.mark.parametrize("completed", [True, False])
async def test_payload_target_never_forces_protected_or_unfinished_results(
    repaired_compile, completed
):
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention, DiagnosticRetentionPolicy

    root, identity, _, context, _ = repaired_compile
    retention = DiagnosticRetention(
        root, identity, policy=DiagnosticRetentionPolicy(payload_target_bytes=1)
    )
    preview = retention.preview()
    assert preview.candidate_count > 0 and preview.over_target_bytes > 0
    assert preview.protected_count > 0
    if completed:
        assert retention.archive().archived_count > 0
        remaining = retention.preview()
        assert remaining.over_target_bytes > 0 and remaining.protected_count > 0
    else:
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_executions SET state = 'failed' WHERE execution_id = ?",
                (context.storage_execution.execution_id,),
            )
    assert retention.preview().candidate_count == 0
    assert retention.archive().archived_count == 0


async def test_accepted_response_links_protect_a_replaced_diagnostic(tmp_path):
    from ultrakb.storage.blobs import read_bytes
    from ultrakb.storage.checkpoint_store import CompileWork
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention

    async with compiler_case(tmp_path, retain_summary=True) as case:
        root, identity, target, context, service = case
        with storage_stage(root, identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT w.work_id, w.model_budget_id, w.object_digest, r.response_id, "
                "r.request_digest, r.content_digest, r.metadata_digest "
                "FROM ukb_compile_work w JOIN ukb_compile_responses r USING(work_id) "
                "WHERE r.step_key = 'short-summary' ORDER BY r.created_at DESC LIMIT 1"
            ).fetchone()
            body = read_bytes(db, row[5]).decode()
            metadata = json.loads(read_bytes(db, row[6]))
            request_id = db.connection.execute(
                "SELECT r.request_id FROM ukb_compile_invocations i "
                "JOIN ukb_stage_attempts s ON s.work_key = 'compile:' || i.invocation_id "
                "JOIN ukb_usage_requests r ON r.stage_attempt_id = s.stage_attempt_id "
                "WHERE i.work_id = ? AND i.step_key = 'short-summary' "
                "AND i.request_digest = ? ORDER BY r.sequence LIMIT 1",
                (row[0], row[4]),
            ).fetchone()[0]
            accepted = db.connection.execute(
                "SELECT metadata_json FROM ukb_compile_steps WHERE work_id = ? "
                "AND step_key = 'short-summary'",
                (row[0],),
            ).fetchone()[0]
            attributes = json.loads(read_bytes(db, json.loads(accepted)["metadata_digest"]))
            assert attributes["reused_response"]["record_file"] == "compile-response/" + row[3]
        # Re-deliver the actual retained request at the storage callback boundary.
        # The original is now non-latest but accepted-step provenance still uses it.
        work = CompileWork(root, identity, *row[:3], context.storage_execution.execution_id)
        work.record_invalid_response(
            "short-summary",
            row[4],
            body,
            **{**metadata, "provider_request_attempt_id": request_id},
        )
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_compile_responses SET created_at = '2020-01-01T00:00:00Z'"
            )
        archived = DiagnosticRetention(root, identity).archive()
        assert archived.archived_count > 0
        with storage_stage(root, identity, read_only=True) as db:
            assert db.connection.execute(
                "SELECT 1 FROM ukb_compile_responses WHERE response_id = ?", (row[3],)
            ).fetchone()
        recovered = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            scope=storage_scope(root, identity),
            context=context,
        )
        assert recovered.status == "compiled" and len(service.requests) == 3


async def test_new_completed_execution_also_invalidates_an_old_selection(
    repaired_compile, monkeypatch
):
    from contextlib import contextmanager

    from ultrakb.storage.database import Database
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, _, context, _ = repaired_compile
    before = read_knowledge_task(root, context.storage_execution.task_id)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("UPDATE ukb_compile_responses SET created_at = '2020-01-01T00:00:00Z'")
    transaction, injected = Database.transaction, False

    @contextmanager
    def continue_before_archive(database):
        nonlocal injected
        if not injected:
            injected = True
            store = ExecutionStore(root, identity)
            execution = store.begin(
                "model_task",
                fingerprint=before.task.fingerprint,
                max_calls=before.task.budget.max_calls,
                deadline_at=before.task.budget.deadline_at,
                resume=before.execution_id,
            )
            store.finish(execution.execution_id, state="completed")
        with transaction(database) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", continue_before_archive)
    result = DiagnosticRetention(root, identity).archive()
    assert injected and result.archived_count == 0
    assert DiagnosticRetention(root, identity).archive().archived_count > 0


async def test_unpublished_review_material_keeps_diagnostics_protected(repaired_compile):
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention, DiagnosticRetentionPolicy
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _, _, _ = repaired_compile
    knowledge = KnowledgeStore(root, identity)
    knowledge.prepare_changes(knowledge.bind(), {"concepts/review.md": b"Unconfirmed review draft"})
    retention = DiagnosticRetention(
        root, identity, policy=DiagnosticRetentionPolicy(payload_target_bytes=1)
    )
    assert retention.preview().candidate_count == 0
    assert retention.archive().status == "blocked"


async def test_missing_accepted_metadata_blocks_diagnostic_retirement(repaired_compile):
    from ultrakb.storage.blobs import blob_path
    from ultrakb.storage.diagnostic_retention import DiagnosticRetention, DiagnosticRetentionPolicy

    root, identity, _, _, _ = repaired_compile
    with storage_stage(root, identity, read_only=True) as db:
        digest = db.connection.execute(
            "SELECT json_extract(metadata_json, '$.metadata_digest') FROM ukb_compile_steps LIMIT 1"
        ).fetchone()[0]
    original = blob_path(root, digest)
    unavailable = original.with_name(original.name + ".unavailable")
    original.rename(unavailable)
    try:
        retention = DiagnosticRetention(
            root, identity, policy=DiagnosticRetentionPolicy(payload_target_bytes=1)
        )
        assert retention.preview().blocked_reason == "accepted_metadata_unavailable"
        result = retention.archive()
        assert result.status == "blocked" and result.archived_count == 0
    finally:
        unavailable.rename(original)


async def test_cancel_after_one_actual_metadata_read_stops_before_scanning_the_rest(
    repaired_compile, monkeypatch
):
    from threading import Event

    import ultrakb.storage.diagnostic_retention as module
    from ultrakb.locks import LockCancelled

    root, identity, _, _, _ = repaired_compile
    cancelled, reads = Event(), []
    original = module.read_bytes

    def cancel_after_read(*args, **options):
        result = original(*args, **options)
        reads.append(True)
        cancelled.set()
        return result

    monkeypatch.setattr(module, "read_bytes", cancel_after_read)
    with pytest.raises(LockCancelled):
        module.DiagnosticRetention(root, identity).archive(cancelled=cancelled.is_set)
    assert len(reads) == 1


@pytest.mark.parametrize("damage", ["missing_reference", "invalid_id", "invalid_digest"])
async def test_unknown_accepted_reference_shapes_block_retirement(
    repaired_compile, monkeypatch, damage
):
    import ultrakb.storage.diagnostic_retention as module

    root, identity, _, context, _ = repaired_compile
    before = read_knowledge_task(root, context.storage_execution.task_id, limit=128)
    with storage_stage(root, identity, read_only=True) as db:
        response_id, digest, execution_id = db.connection.execute(
            "SELECT response_id, content_digest, execution_id FROM ukb_compile_responses LIMIT 1"
        ).fetchone()
    original = module.read_bytes

    def malformed_metadata(*args, **options):
        value = json.loads(original(*args, **options))
        if damage == "missing_reference":
            value.pop("reused_response")
        else:
            value["reused_response"] = {
                "record_file": "compile-response/" + response_id,
                "response_file": "blob/" + digest,
                "response_sha256": digest,
                "execution_id": execution_id,
                "awaiting_validation": True,
            }
            if damage == "invalid_id":
                value["reused_response"]["record_file"] = "compile-response/invalid"
            else:
                value["reused_response"]["response_file"] = "blob/invalid"
        return json.dumps(value).encode()

    monkeypatch.setattr(module, "read_bytes", malformed_metadata)
    retention = module.DiagnosticRetention(
        root, identity, policy=module.DiagnosticRetentionPolicy(payload_target_bytes=1)
    )
    result = retention.archive()
    assert result.status == "blocked" and result.archived_count == 0
    after = read_knowledge_task(root, context.storage_execution.task_id, limit=128)
    assert after.task == before.task and after.requests == before.requests
