"""Authenticate explicit summary continuation without resetting paid attempts."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ultrakb.llm_usage import active_scope, read_requests
from ultrakb.llm_usage_models import UsageExecution
from ultrakb.model_task import read_task

_RETRY: ContextVar[tuple[Path, str] | None] = ContextVar("interrupted_compile_retry", default=None)
_FAILED: ContextVar[dict] = ContextVar("failed_summary_retry", default={})


def _failed_publications(root: Path, source_id: str, view_id: str) -> dict:
    from ultrakb.ingest_records import UnitPublication, UnitRevision
    from ultrakb.source_catalog import read_record, read_source, record_path
    from ultrakb.state import HashRegistry
    from ultrakb.unit_publication import list_source_units, read_unit_publication

    source = read_source(root, source_id)
    result = {}
    for unit in list_source_units(root, source_id):
        state = read_unit_publication(root, unit.unit_id, view_id)
        revision = read_record(root, "unit-revisions", unit.target_revision_id, UnitRevision)
        attempt = read_record(root, "attempts", state.attempt_id, UnitPublication)
        if (
            state.status == "failed"
            and state.stage == "compilation"
            and "summary" in state.unfinished
            and state.target_revision_id == unit.target_revision_id
            and state == attempt
            and state.job_id == revision.job_id
            and revision.unit_id == unit.unit_id
            and revision.source_revision_id == source.target_revision_id
        ):
            result[unit.unit_id] = {
                "publication_id": state.publication_id,
                "attempt_id": state.attempt_id,
                "attempt_sha256": HashRegistry.hash_file(
                    record_path(root, "attempts", state.attempt_id)
                ),
                "job_id": state.job_id,
                "target_revision_id": state.target_revision_id,
                "view_id": view_id,
                "quality": list(state.quality),
            }
    return result


@contextmanager
def explicit_compile_retry(
    root: Path, source_id: str, *, retry_failed_response: bool = False, view_id: str = "legacy"
):
    """Only the public source retry may admit recovery; ordinary compilation cannot."""
    failed = _failed_publications(root, source_id, view_id) if retry_failed_response else {}
    token = _RETRY.set((root.resolve(), source_id))
    failed_token = _FAILED.set(failed)
    try:
        yield
    finally:
        _FAILED.reset(failed_token)
        _RETRY.reset(token)


def _retry_request(root: Path, task_id: str, stage: str):
    scope = active_scope()
    if (
        scope is None
        or _RETRY.get() != (root.resolve(), scope.source_id)
        or scope.kb_dir != root.resolve()
        or not all(
            (scope.source_id, scope.source_revision_id, scope.unit_id, scope.unit_revision_id)
        )
    ):
        return None

    def execution(identity: str) -> UsageExecution:
        path = root / ".ultrakb/usage/executions" / (identity + ".json")
        record = UsageExecution.model_validate_json(path.read_text("utf-8"))
        if record.execution_id != identity:
            raise ValueError("Interrupted compile execution identity differs")
        return record

    current = execution(scope.execution_id)
    if (
        current.operation != "retry_source"
        or current.state != "started"
        or current.model_task_id != task_id
        or scope.source_id not in current.source_ids
    ):
        return None
    task = read_task(root, task_id)
    if task.inputs.get(scope.source_id) != scope.source_revision_id:
        return None
    records = [
        item
        for item in read_requests(root)
        if item.source_id == scope.source_id
        and item.source_revision_id == scope.source_revision_id
        and item.unit_id == scope.unit_id
        and item.unit_revision_id == scope.unit_revision_id
        and item.stage == stage
    ]
    if not records:
        return None
    latest = max(item.started_at for item in records)
    newest = [item for item in records if item.started_at == latest]
    if len(newest) != 1:
        return None
    request = newest[0]
    previous = execution(request.execution_id)
    if (
        request.execution_id == scope.execution_id
        or scope.source_id not in previous.source_ids
        or previous.model_task_id != task_id
        or request.operation != "compile"
        or request.policy_fingerprint != task.fingerprint
        or request.model != task.bindings["models"]["compile"]
        or request.send_status != "sent"
        or not request.logical_call_id
    ):
        return None
    return scope, request, previous


def _receipt(request, stage: str) -> dict:
    return {
        "request_attempt_id": request.request_attempt_id,
        "logical_call_id": request.logical_call_id,
        "execution_id": request.execution_id,
        "stage": stage,
    }


def interrupted_repair_request(root: Path, task_id: str, stage: str) -> dict | None:
    match = _retry_request(root, task_id, stage)
    if match is None:
        return None
    scope, request, previous = match
    publication = _FAILED.get().get(scope.unit_id)
    if publication and previous.state != "completed" and request.state == "completed":
        # Legacy interrupted deliveries could record a paid HTTP response but
        # lose its in-memory body before validation. The caller proves no
        # accepted result/failure exists and consumes this recovery exactly once.
        return {
            **_receipt(request, stage),
            "outcome": "received_delivery_not_persisted",
            "publication": publication,
        }
    if (
        previous.state == "completed"
        or request.observation != "call"
        or request.state != "started"
        or request.ended_at is not None
        or request.business_state is not None
        or request.provider_request_id is not None
    ):
        return None
    # Legacy usage has no prompt digest. The caller also requires exact
    # checkpoint conditions/key/digest and records this association atomically.
    return _receipt(request, stage)


def failed_summary_request(root: Path, task_id: str, stage: str) -> dict | None:
    from ultrakb.source_catalog import record_path
    from ultrakb.state import HashRegistry

    if not _FAILED.get() or not (
        stage == "compile.summary" or stage.startswith("compile.document-summary:")
    ):
        return None
    match = _retry_request(root, task_id, stage)
    if match is None:
        return None
    scope, request, previous = match
    publication = _FAILED.get().get(scope.unit_id)
    if (
        publication is None
        or publication["target_revision_id"] != scope.unit_revision_id
        or publication["job_id"] != scope.attempt_id
        or publication["job_id"] != request.attempt_id
        or HashRegistry.hash_file(record_path(root, "attempts", publication["attempt_id"]))
        != publication["attempt_sha256"]
        or previous.state != "completed"
        or request.state != "completed"
        or request.ended_at is None
        or request.business_state != "completed"
    ):
        return None
    return {**_receipt(request, stage), "publication": publication}
