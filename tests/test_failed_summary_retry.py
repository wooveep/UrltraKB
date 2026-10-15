"""Explicit recovery uses the latest failed publication and never resets its ledger."""

import json

import pytest


@pytest.mark.parametrize("accepted_retry", [True, False])
def test_public_failed_response_retry_is_opt_in_and_consumed_once(kb_dir, accepted_retry):
    from publication_recovery_inputs import failed_summary_inputs, ledger_bytes, retrying

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.locks import atomic_write_text
    from ultrakb.model_task import read_task

    normal = kb_dir / "wiki/summaries/normal.md"
    atomic_write_text(normal, "Normal operation requires supervision.")
    retained = normal.read_bytes()
    fixture = failed_summary_inputs(kb_dir)
    old_ledger = ledger_bytes(fixture)
    with retrying(fixture), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        fixture.checkpoint.claim_generation(
            "short-summary", "same-request", stage="compile.summary"
        )
    with retrying(fixture, retry_failed_response=True):
        fixture.checkpoint.claim_generation(
            "short-summary", "same-request", stage="compile.summary"
        )
        if accepted_retry:
            accepted = validate_page_delivery(
                json.dumps(
                    {"description": "Operating condition", "content": "Supervision required."}
                ),
                response_protocol="summary-page-json",
            )
            fixture.checkpoint.save("short-summary", "same-request", accepted)
        else:
            with pytest.raises(PageResponseError):
                validate_page_delivery("{}", response_protocol="summary-page-json")
            fixture.checkpoint.record_failure(
                "short-summary", "same-request", "summary_invalid_json"
            )
    entry = json.loads(fixture.checkpoint.path.read_text())["entries"]["short-summary"]
    assert entry["manual_retry_used"] is True and entry["repair_used"] is True
    assert (
        entry["manual_retry"]["publication"]["target_revision_id"]
        == fixture.state.target_revision_id
    )
    assert read_task(kb_dir, fixture.task.task_id).used == 4
    assert len(list((kb_dir / ".ultrakb/model-tasks").glob("*.json"))) == 1
    assert all(path.read_bytes() == data for path, data in old_ledger.items())
    assert normal.read_bytes() == retained
    restored = CompileCheckpoints(
        kb_dir, fixture.task, {"document": "failed"}, fixture.checkpoint.conditions
    )
    if accepted_retry:
        assert "Supervision required" in restored.load("short-summary", "same-request")
    else:
        with (
            retrying(fixture, retry_failed_response=True),
            pytest.raises(PageResponseError, match="compile_repair_exhausted"),
        ):
            restored.claim_generation("short-summary", "same-request", stage="compile.summary")
        assert restored.load("short-summary", "same-request") is None


def test_latest_failed_publication_cannot_be_paired_with_older_task_response(kb_dir):
    from publication_recovery_inputs import failed_summary_inputs, ledger_bytes, retrying

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compilation_report import CompilationIncomplete
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.llm_usage_models import UsageExecution
    from ultrakb.locks import atomic_write_json, kb_ingest_lock
    from ultrakb.model_task import task_path
    from ultrakb.source_catalog import read_record
    from ultrakb.unit_publication import begin_unit_attempt, list_source_units, record_unit_failure

    fixture = failed_summary_inputs(kb_dir)
    [unit] = list_source_units(kb_dir, fixture.result.source_id)
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        state, runnable = begin_unit_attempt(kb_dir, unit, revision, recompile=True)
        assert runnable
        latest = record_unit_failure(
            kb_dir,
            state,
            "compilation",
            CompilationIncomplete(("summary_invalid_json",), ("summary",)),
        )
    assert latest.attempt_id != fixture.state.attempt_id
    assert latest.target_revision_id == fixture.state.target_revision_id
    newer_task = fixture.task.model_copy(update={"task_id": "7" * 32})
    atomic_write_json(task_path(kb_dir, newer_task.task_id), newer_task.model_dump(mode="json"))
    newer_execution = UsageExecution(
        execution_id="8" * 32,
        operation="import_document",
        state="completed",
        model_task_id=newer_task.task_id,
        source_ids=[fixture.result.source_id],
    )
    atomic_write_json(
        kb_dir / ".ultrakb/usage/executions" / (newer_execution.execution_id + ".json"),
        newer_execution.model_dump(mode="json"),
    )
    newer_request = fixture.request.model_copy(
        update={
            "request_attempt_id": "9" * 32,
            "logical_call_id": "a" * 32,
            "execution_id": newer_execution.execution_id,
            "started_at": "2026-10-12T00:00:02+00:00",
            "ended_at": "2026-10-12T00:00:03+00:00",
        }
    )
    atomic_write_json(
        fixture.request_path.parent / (newer_request.request_attempt_id + ".json"),
        newer_request.model_dump(mode="json"),
    )
    old_ledger = ledger_bytes(fixture)
    before = fixture.checkpoint.path.read_bytes()
    with (
        retrying(fixture, retry_failed_response=True),
        pytest.raises(PageResponseError, match="compile_repair_exhausted"),
    ):
        fixture.checkpoint.claim_generation(
            "short-summary", "same-request", stage="compile.summary"
        )
    assert fixture.checkpoint.path.read_bytes() == before
    assert all(path.read_bytes() == data for path, data in old_ledger.items())
