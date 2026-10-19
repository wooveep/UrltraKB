"""New-format execution and receipt durability through the real shared store."""

from uuid import uuid4

import pytest


def test_partial_late_usage_survives_publication_failure_and_restart(tmp_path):
    from ultrakb.llm_usage import UsageRequest
    from ultrakb.storage.checkpoint_store import CompileCheckpointStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "kb"
    identity = create_database(root)
    execution = ExecutionStore(root, identity).begin("import", fingerprint="a" * 64, max_calls=2)
    store = UsageStore(root, identity)
    checkpoints = CompileCheckpointStore(root, identity).for_work(
        execution.model_budget_id,
        object_id={"unit": "worksheet"},
        conditions={"policy": "v1"},
        execution_id=execution.execution_id,
    )
    checkpoints.save(
        "summary:0",
        "b" * 64,
        {"summary": "already accepted"},
        metadata={"source_window": "retained native context " * 4000},
    )
    request = UsageRequest(
        request_attempt_id=uuid4().hex,
        logical_call_id=uuid4().hex,
        execution_id=execution.execution_id,
        model="test/model",
        stage="compile.summary:0",
        started_at="2026-10-18T00:00:00+00:00",
        observation="transport",
    )
    store.begin(request, model_budget_id=execution.model_budget_id, stage_code="compile.summary")
    store.merge(request.request_attempt_id, version=1, send_status="sent", input_total=30)
    knowledge = KnowledgeStore(root, identity)
    candidate = knowledge.prepare_revision({"index.md": b"candidate"}, expected=None)
    other = knowledge.publish(knowledge.prepare_revision({"index.md": b"other"}, expected=None))
    with pytest.raises(PublicationConflict):
        knowledge.publish(candidate)
    restarted = UsageStore(root, identity)
    restarted.merge(
        request.request_attempt_id,
        version=3,
        state="cancelled",
        output_total=7,
        cached_input=10,
        reasoning_output=2,
    )
    # Repeated and older partial receipts cannot erase known values or undo a send.
    restarted.merge(request.request_attempt_id, version=1, input_total=20, send_status="not_sent")
    restarted.merge(request.request_attempt_id, version=3, output_total=7, input_total=None)
    receipt = restarted.summary(execution_id=execution.execution_id)
    assert receipt["requests"] == receipt["actual_sends"] == 1
    assert receipt["input_total"] == 30
    assert receipt["output_total"] == 7
    assert receipt["total_known"] == 37
    assert receipt["cached_input"] == 10
    assert receipt["reasoning_output"] == 2
    assert receipt["usage_complete"]
    assert ExecutionStore(root, identity).budget(execution.model_budget_id).used == 1
    recovered = CompileCheckpointStore(root, identity).for_work(
        execution.model_budget_id,
        object_id={"unit": "worksheet"},
        conditions={"policy": "v1"},
        execution_id=execution.execution_id,
    )
    assert recovered.load("summary:0", "b" * 64) == {"summary": "already accepted"}
    assert knowledge.bind(other.publication_id).read("index.md") == b"other"


def test_receipt_field_versions_merge_independently_and_retry_keeps_spent_budget(tmp_path):
    from ultrakb.llm_usage import UsageRequest
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "kb"
    identity = create_database(root)
    executions, usage = ExecutionStore(root, identity), UsageStore(root, identity)
    first = executions.begin("import", fingerprint="a" * 64, max_calls=1)
    request = UsageRequest(
        request_attempt_id=uuid4().hex,
        logical_call_id=uuid4().hex,
        execution_id=first.execution_id,
        model="test/model",
        stage="compile.summary:1",
        started_at="2026-10-18T00:00:00+00:00",
    )
    usage.begin(request, model_budget_id=first.model_budget_id, stage_code="compile.summary")
    usage.merge(request.request_attempt_id, version=1, send_status="sent", input_total=10)
    usage.merge(request.request_attempt_id, version=4, state="failed", output_total=4)
    usage.merge(request.request_attempt_id, version=2, input_total=12)
    assert usage.request(request.request_attempt_id).input_total == 12
    executions.finish(first.execution_id, state="failed")
    retry = executions.begin("import", fingerprint="a" * 64, max_calls=1, resume=first.execution_id)
    assert retry.execution_id != first.execution_id
    assert retry.task_id == first.task_id
    assert retry.model_budget_id == first.model_budget_id
    assert executions.budget(retry.model_budget_id).used == 1
    next_request = request.model_copy(
        update={"request_attempt_id": uuid4().hex, "execution_id": retry.execution_id}
    )
    from ultrakb.llm_execution import ModelBudgetExceeded

    with pytest.raises(ModelBudgetExceeded):
        usage.begin(
            next_request, model_budget_id=retry.model_budget_id, stage_code="compile.summary"
        )
    assert usage.summary(execution_id=retry.execution_id)["requests"] == 0


def test_received_response_is_repaired_without_another_send(tmp_path, monkeypatch):
    import sqlite3

    from ultrakb.llm_usage import UsageRequest
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "kb"
    identity = create_database(root)
    execution = ExecutionStore(root, identity).begin("import", fingerprint="a" * 64, max_calls=1)
    request = UsageRequest(
        request_attempt_id=uuid4().hex,
        execution_id=execution.execution_id,
        model="test/model",
        stage="compile.summary:2",
        started_at="2026-10-18T00:00:00+00:00",
    )
    usage = UsageStore(root, identity)
    usage.begin(request, model_budget_id=execution.model_budget_id, stage_code="compile.summary")
    receipts = ReceiptStore(root, identity)
    with monkeypatch.context() as failed:

        def busy(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        failed.setattr(UsageStore, "merge", busy)
        result = receipts.capture(
            request.request_attempt_id,
            b"already paid provider response",
            version=1,
            usage={"input_total": 15, "output_total": 4, "state": "completed"},
        )
        assert result.requires_repair
    repaired = ReceiptStore(root, identity).repair(limit=10)
    assert repaired.repaired == 1
    assert repaired.remaining == 0
    assert receipts.read(request.request_attempt_id) == b"already paid provider response"
    receipt = usage.summary(execution_id=execution.execution_id)
    assert receipt["requests"] == receipt["actual_sends"] == 1
    assert receipt["input_total"] == 15
    assert receipt["output_total"] == 4
    assert ExecutionStore(root, identity).budget(execution.model_budget_id).used == 1


def test_received_response_is_retained_while_blob_retirement_needs_repair(tmp_path, monkeypatch):
    from ultrakb.llm_usage import UsageRequest
    from ultrakb.storage import maintenance
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "kb"
    identity = create_database(root)
    execution = ExecutionStore(root, identity).begin("import", fingerprint="a" * 64, max_calls=1)
    request = UsageRequest(
        request_attempt_id=uuid4().hex,
        execution_id=execution.execution_id,
        model="test/model",
        stage="compile.summary:3",
        started_at="2026-10-18T00:00:00+00:00",
    )
    UsageStore(root, identity).begin(
        request, model_budget_id=execution.model_budget_id, stage_code="compile.summary"
    )
    response = b"paid response matching interrupted orphan cleanup"
    with storage_stage(root, identity) as db:
        put_blob(db, response)
    with monkeypatch.context() as failed:

        def interrupted(*args, **kwargs):
            raise OSError("interrupted unlink")

        failed.setattr(maintenance, "durable_unlink", interrupted)
        with pytest.raises(OSError, match="interrupted unlink"):
            maintenance.Maintenance(root, identity).collect()
    receipts = ReceiptStore(root, identity)
    assert receipts.capture(
        request.request_attempt_id, response, version=1, usage={}
    ).requires_repair
    assert receipts.repair().remaining == 1
    maintenance.Maintenance(root, identity).collect()
    assert receipts.repair().repaired == 1
    assert receipts.read(request.request_attempt_id) == response


def test_restore_rejects_old_request_and_dispatch_tokens_even_with_a_new_store(tmp_path):
    from ultrakb.llm_usage import UsageRequest
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.maintenance import Maintenance
    from ultrakb.storage.pending_store import PendingStore
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    root = tmp_path / "kb"
    identity = create_database(root)
    source = tmp_path / "input.md"
    source.write_text("managed input")
    revision = CatalogStore(root, identity).admit(source, source_key="input")
    pending = PendingStore(root, identity)
    job = pending.enqueue(revision.revision_id)
    dispatched = pending.claim()
    execution = ExecutionStore(root, identity).begin("import", fingerprint="a" * 64, max_calls=1)
    request = UsageRequest(
        request_attempt_id=uuid4().hex,
        execution_id=execution.execution_id,
        model="test/model",
        stage="compile.summary:0",
        started_at="2026-10-18T00:00:00+00:00",
    )
    UsageStore(root, identity).begin(
        request, model_budget_id=execution.model_budget_id, stage_code="compile.summary"
    )
    archive = tmp_path / "backup"
    Maintenance(root, identity).backup(archive)
    restored = tmp_path / "restored"
    new_identity = Maintenance.restore(archive, restored)
    assert PendingStore(restored, new_identity).start(dispatched) is None
    assert PendingStore(restored, new_identity).attempts(job.intent_id) == ()
    with pytest.raises(ValueError, match="another generation"):
        UsageStore(restored, new_identity).merge(
            request.request_attempt_id, version=1, send_status="sent", input_total=20
        )
    with pytest.raises(ValueError, match="another generation"):
        UsageStore(restored, new_identity).begin(
            request, model_budget_id=execution.model_budget_id, stage_code="compile.summary"
        )
    receipts = ReceiptStore(restored, new_identity)
    with pytest.raises(ValueError, match="another generation"):
        receipts.capture(request.request_attempt_id, b"old late response", version=1, usage={})
    with pytest.raises(KeyError):
        receipts.read(request.request_attempt_id)
    assert not list((restored / ".ultrakb/receipts").glob("**/*.json"))
    assert (
        UsageStore(restored, new_identity).request(request.request_attempt_id).input_total is None
    )
