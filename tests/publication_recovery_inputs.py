"""Persist historical recovery inputs through real source and publication records."""

from contextlib import contextmanager
from types import SimpleNamespace

from publication_fixtures import fail_document

from ultrakb.compilation_report import CompilationIncomplete
from ultrakb.compile_checkpoints import CompileCheckpoints
from ultrakb.compile_recovery import explicit_compile_retry
from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_usage import _SCOPE, UsageRequest, UsageScope
from ultrakb.llm_usage_models import UsageExecution
from ultrakb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
from ultrakb.model_task import ModelTask, read_task, source_task, task_path
from ultrakb.unit_publication import read_unit_publication, record_unit_failure


def failed_summary_inputs(kb_dir, *, unknown=False):
    """Historical states are domain inputs; this fixture makes no provider call."""
    source = kb_dir / "failed.txt"
    atomic_write_text(source, "The operation requires supervision.")
    result = fail_document(kb_dir, source, bundle=LlmCredentialBundle())
    unit = result.units[0]
    state = read_unit_publication(kb_dir, unit.unit_id)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        state = record_unit_failure(
            kb_dir,
            state,
            "compilation",
            CompilationIncomplete(("summary_invalid_json",), ("summary",)),
        )
    old_id = source_task(kb_dir, result.source_id, result.source_revision_id)
    previous = read_task(kb_dir, old_id)
    task = ModelTask(
        task_id=old_id,
        kb_generation=previous.kb_generation,
        fingerprint="2" * 64,
        bindings={"models": {"compile": "deepseek/deepseek-flash"}},
        policy={"max_calls": 6},
        used=4,
        stages={"compile": 4},
        inputs={result.source_id: result.source_revision_id},
    )
    atomic_write_json(task_path(kb_dir, old_id), task.model_dump(mode="json"))
    previous_execution, current_execution = "3" * 32, "4" * 32
    for identity, operation in (
        (previous_execution, "import_document"),
        (current_execution, "retry_source"),
    ):
        execution = UsageExecution(
            execution_id=identity,
            operation=operation,
            state="started" if unknown or identity == current_execution else "completed",
            model_task_id=old_id,
            source_ids=[result.source_id],
        )
        atomic_write_json(
            kb_dir / ".ultrakb/usage/executions" / f"{identity}.json",
            execution.model_dump(mode="json"),
        )
    request = UsageRequest(
        request_attempt_id="5" * 32,
        logical_call_id="6" * 32,
        execution_id=previous_execution,
        source_id=result.source_id,
        source_revision_id=result.source_revision_id,
        unit_id=unit.unit_id,
        unit_revision_id=unit.target_revision_id,
        attempt_id=state.job_id,
        operation="compile",
        policy_fingerprint=task.fingerprint,
        model="deepseek/deepseek-flash",
        stage="compile.summary",
        started_at="2026-10-12T00:00:00+00:00",
        send_status="sent",
        observation="call",
        **(
            {}
            if unknown
            else {
                "state": "completed",
                "business_state": "completed",
                "ended_at": "2026-10-12T00:00:01+00:00",
            }
        ),
    )
    request_path = kb_dir / ".ultrakb/usage/requests" / f"{request.request_attempt_id}.json"
    atomic_write_json(request_path, request.model_dump(mode="json"))
    scope = UsageScope(
        kb_dir,
        current_execution,
        source_id=result.source_id,
        source_revision_id=result.source_revision_id,
        unit_id=unit.unit_id,
        unit_revision_id=unit.target_revision_id,
        attempt_id=state.job_id,
    )
    checkpoint = CompileCheckpoints(
        kb_dir, task, {"document": "failed"}, {"protocol": "summary-page-json"}
    )
    checkpoint.claim_repair("short-summary", "same-request")
    if not unknown:
        checkpoint.record_failure("short-summary", "same-request", "summary_invalid_json")
    return SimpleNamespace(
        root=kb_dir,
        result=result,
        state=state,
        task=task,
        scope=scope,
        request=request,
        request_path=request_path,
        checkpoint=checkpoint,
    )


@contextmanager
def retrying(fixture, *, retry_failed_response=False):
    token = _SCOPE.set(fixture.scope)
    try:
        with explicit_compile_retry(
            fixture.root,
            fixture.scope.source_id,
            retry_failed_response=retry_failed_response,
        ):
            yield
    finally:
        _SCOPE.reset(token)


def ledger_bytes(fixture):
    return {
        p: p.read_bytes()
        for directory in (".ultrakb/model-tasks", ".ultrakb/usage/requests")
        for p in (fixture.root / directory).glob("*.json")
    }
