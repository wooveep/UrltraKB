"""Associate an explicit summary retry with one unobserved, already charged send."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from openkb.llm_usage import active_scope, read_requests
from openkb.llm_usage_models import UsageExecution
from openkb.model_task import read_task

_RETRY: ContextVar[tuple[Path, str] | None] = ContextVar("interrupted_compile_retry", default=None)


@contextmanager
def explicit_compile_retry(root: Path, source_id: str):
    """Only the public source retry may admit recovery; ordinary compilation cannot."""
    token = _RETRY.set((root.resolve(), source_id))
    try:
        yield
    finally:
        _RETRY.reset(token)


def interrupted_repair_request(root: Path, task_id: str, stage: str) -> dict | None:
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
        path = root / ".openkb/usage/executions" / (identity + ".json")
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
        and execution(item.execution_id).model_task_id == task_id
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
        or previous.state == "completed"
        or request.operation != "compile"
        or request.policy_fingerprint != task.fingerprint
        or request.model != task.bindings["models"]["compile"]
        or request.state != "started"
        or request.send_status != "sent"
        or request.observation != "call"
        or request.ended_at is not None
        or request.business_state is not None
        or request.provider_request_id is not None
        or not request.logical_call_id
    ):
        return None
    # The legacy usage ledger has no prompt digest. The caller must also match
    # the checkpoint's exact conditions/key/digest, and record this association.
    return {
        "request_attempt_id": request.request_attempt_id,
        "logical_call_id": request.logical_call_id,
        "execution_id": request.execution_id,
        "stage": stage,
    }
