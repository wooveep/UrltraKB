"""Unobserved recovery keeps its historical ledger and never reopens semantic repair."""

import json
from dataclasses import replace

import pytest


@pytest.mark.parametrize("valid_recovery", [True, False])
def test_public_retry_recovers_unobserved_summary_repair_once(kb_dir, valid_recovery):
    from publication_recovery_inputs import failed_summary_inputs, ledger_bytes, retrying

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.llm_usage import UsageRequest
    from ultrakb.llm_usage_models import UsageExecution
    from ultrakb.locks import atomic_write_json, atomic_write_text
    from ultrakb.model_task import read_task

    fixture = failed_summary_inputs(kb_dir, unknown=True)
    normal = kb_dir / "wiki/summaries/normal.md"
    atomic_write_text(normal, "Normal operation requires supervision.")
    retained = normal.read_bytes()
    historical = ledger_bytes(fixture)
    with retrying(fixture):
        fixture.checkpoint.claim_generation(
            "short-summary", "same-request", stage="compile.summary"
        )
    entry = json.loads(fixture.checkpoint.path.read_text())["entries"]["short-summary"]
    assert entry["recovery_used"] is True and entry["repair_used"] is True
    assert entry["recovery"]["request_attempt_id"] == fixture.request.request_attempt_id
    accepted = json.dumps(
        {"description": "Operating condition", "content": "Supervision required."}
    )
    if valid_recovery:
        received = validate_page_delivery(accepted, response_protocol="summary-page-json")
        fixture.checkpoint.save("short-summary", "same-request", received)
    else:
        with pytest.raises(PageResponseError):
            validate_page_delivery(
                '{"description":"Still invalid","content":""}',
                response_protocol="summary-page-json",
            )
        fixture.checkpoint.record_failure("short-summary", "same-request", "summary_invalid_shape")
        with retrying(fixture), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            fixture.checkpoint.claim_generation(
                "short-summary", "same-request", stage="compile.summary"
            )
        # A subsequent observed invalid delivery is a new ledger input. The
        # original unobserved receipt remains immutable and cannot authorize it.
        execution_path = (
            kb_dir / ".ultrakb/usage/executions" / (fixture.scope.execution_id + ".json")
        )
        execution = UsageExecution.model_validate_json(execution_path.read_text())
        atomic_write_json(
            execution_path,
            execution.model_copy(update={"state": "completed"}).model_dump(mode="json"),
        )
        observed = UsageRequest.model_validate(
            fixture.request.model_dump(mode="json")
            | {
                "request_attempt_id": "7" * 32,
                "logical_call_id": "8" * 32,
                "execution_id": fixture.scope.execution_id,
                "state": "completed",
                "business_state": "completed",
                "started_at": "2026-10-12T00:00:02+00:00",
                "ended_at": "2026-10-12T00:00:03+00:00",
            }
        )
        atomic_write_json(
            fixture.request_path.parent / (observed.request_attempt_id + ".json"),
            observed.model_dump(mode="json"),
        )
        current = UsageExecution(
            execution_id="9" * 32,
            operation="retry_source",
            state="started",
            model_task_id=fixture.task.task_id,
            source_ids=[fixture.result.source_id],
        )
        atomic_write_json(
            execution_path.parent / (current.execution_id + ".json"),
            current.model_dump(mode="json"),
        )
        fixture.scope = replace(fixture.scope, execution_id=current.execution_id)
        with retrying(fixture, retry_failed_response=True):
            fixture.checkpoint.claim_generation(
                "short-summary", "same-request", stage="compile.summary"
            )
            fixture.checkpoint.save(
                "short-summary",
                "same-request",
                validate_page_delivery(accepted, response_protocol="summary-page-json"),
            )
        latest = json.loads(fixture.checkpoint.path.read_text())["entries"]["short-summary"]
        assert latest["manual_retry_used"] is True
        assert latest["manual_retry"]["request_attempt_id"] == observed.request_attempt_id
    restored = CompileCheckpoints(
        kb_dir, fixture.task, {"document": "failed"}, fixture.checkpoint.conditions
    )
    assert "Supervision required" in restored.load("short-summary", "same-request")
    with retrying(fixture), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restored.claim_repair("short-summary", "same-request")
    assert all(path.read_bytes() == data for path, data in historical.items())
    assert normal.read_bytes() == retained
    assert read_task(kb_dir, fixture.task.task_id).used == 4
    assert len(list((kb_dir / ".ultrakb/model-tasks").glob("*.json"))) == 1
