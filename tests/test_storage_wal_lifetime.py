"""Repeated actual application callbacks reuse WAL space without pinning reclamation."""

from uuid import uuid4

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.application.task_stages import task_stage
from ultrakb.llm_usage import UsageRequest
from ultrakb.storage.coordination import storage_stage, try_reclamation
from ultrakb.storage.database import database_path
from ultrakb.storage.execution_store import now
from ultrakb.storage.usage_audit import UsageAudit
from ultrakb.storage.usage_store import UsageStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("model_operation", [True, False])
def test_callbacks_reuse_wal_space_without_holding_a_reclamation_lease(tmp_path, model_operation):
    root, identity, _ = question_knowledge(tmp_path)
    (root / ".env").write_text(
        "LLM_API_KEY=offline-test-key\nOPENAI_API_BASE=http://127.0.0.1:9/v1\n"
    )
    context = ExecutionContext(storage_identity=identity)
    operation = (
        context.begin(root)
        if model_operation
        else begin_storage_operation(root, context, "maintenance-test")
    )
    usage = UsageStore(root, identity)
    with operation:
        for _ in range(300):
            if model_operation:
                execution = context.storage_execution
                request = UsageRequest(
                    request_attempt_id=uuid4().hex,
                    execution_id=execution.execution_id,
                    model="synthetic-known-unsent",
                    stage="wal-calibration",
                    started_at=now(),
                )
                usage.begin(
                    request,
                    model_budget_id=execution.model_budget_id,
                    stage_code="wal.calibration.v1",
                )
                usage.merge(
                    request.request_attempt_id,
                    version=1,
                    state="failed",
                    send_status="not_sent",
                    input_total=0,
                    output_total=0,
                    observation="transport",
                )
            else:
                with task_stage(context, "maintenance.calibration.v1", root=root):
                    pass
        # Actual access still leases each stage; idle model/operation time does not.
        with try_reclamation(root, identity) as lease:
            assert lease is not None
        with storage_stage(root, identity, read_only=True) as db:
            page_size = db.connection.execute("PRAGMA page_size").fetchone()[0]
            threshold = db.connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        # Default checkpointing should recycle around its threshold. Allow one
        # bounded transaction's frames, independent of machine timing/fsync speed.
        limit = 32 + (threshold + 128) * (page_size + 24)
        database = database_path(root)
        assert database.with_name(database.name + "-wal").stat().st_size <= limit
        context._storage_result = "completed"
    audit = UsageAudit(root, identity).run()
    assert audit.status == "consistent"
    assert audit.request_count == (300 if model_operation else 0)
