"""Unknown transport recovery preserves the original bounded repair and ledger."""

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openkb.agent.page_response import PageResponseError
from openkb.compile_checkpoints import CompileCheckpoints
from openkb.compile_recovery import explicit_compile_retry
from openkb.lifecycle import current_generation
from openkb.llm_usage import _SCOPE, UsageRequest, UsageScope
from openkb.llm_usage_models import UsageExecution
from openkb.model_task import ModelTask


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


@pytest.fixture
def interrupted_summary(kb_dir):
    source, revision, unit, unit_revision = (c * 32 for c in "abcd")
    task_id, old_execution, current_execution = (c * 32 for c in "ef1")
    task = ModelTask(
        task_id=task_id,
        kb_generation=current_generation(kb_dir),
        fingerprint="2" * 64,
        bindings={"models": {"compile": "openai/gpt-4o-mini"}},
        policy={"max_calls": 10},
        used=1,
        stages={"compile": 1},
        inputs={source: revision},
    )
    task_path = kb_dir / ".openkb/model-tasks" / f"{task_id}.json"
    write(task_path, task.model_dump(mode="json"))
    for identity, operation in (
        (old_execution, "import_document"),
        (current_execution, "retry_source"),
    ):
        record = UsageExecution(
            execution_id=identity,
            operation=operation,
            state="started",
            model_task_id=task_id,
            source_ids=[source],
        )
        write(
            kb_dir / ".openkb/usage/executions" / f"{identity}.json", record.model_dump(mode="json")
        )
    request = UsageRequest(
        request_attempt_id="3" * 32,
        logical_call_id="4" * 32,
        execution_id=old_execution,
        source_id=source,
        source_revision_id=revision,
        unit_id=unit,
        unit_revision_id=unit_revision,
        operation="compile",
        policy_fingerprint=task.fingerprint,
        model=task.bindings["models"]["compile"],
        stage="compile.document-summary:1",
        started_at="2026-10-12T00:00:00+00:00",
        send_status="sent",
    )
    request_path = kb_dir / ".openkb/usage/requests" / f"{request.request_attempt_id}.json"
    write(request_path, request.model_dump(mode="json"))
    scope = UsageScope(
        kb_dir,
        current_execution,
        source_id=source,
        source_revision_id=revision,
        unit_id=unit,
        unit_revision_id=unit_revision,
    )
    checkpoint = CompileCheckpoints(
        kb_dir, task, {"document": "manual"}, {"protocol": "retained-v1"}
    )
    checkpoint.claim_repair("summary:1", "same-request")
    return SimpleNamespace(
        root=kb_dir,
        task=task,
        task_path=task_path,
        scope=scope,
        request=request,
        request_path=request_path,
        checkpoint=checkpoint,
    )


@contextmanager
def retrying(fixture):
    token = _SCOPE.set(fixture.scope)
    try:
        with explicit_compile_retry(fixture.root, fixture.scope.source_id):
            yield
    finally:
        _SCOPE.reset(token)


def claim(fixture):
    fixture.checkpoint.claim_generation(
        "summary:1", "same-request", stage="compile.document-summary:1"
    )


def test_one_recovery_preserves_old_charge_and_never_reopens_semantic_repair(interrupted_summary):
    f = interrupted_summary
    retained = {path: path.read_bytes() for path in (f.task_path, f.request_path)}
    with retrying(f):
        claim(f)
        entry = json.loads(f.checkpoint.path.read_text())["entries"]["summary:1"]
        assert entry["state"] == "resuming"
        assert entry["repair_used"] is True and entry["recovery_used"] is True
        assert entry["recovery"] == {
            "request_attempt_id": f.request.request_attempt_id,
            "logical_call_id": f.request.logical_call_id,
            "execution_id": f.request.execution_id,
            "stage": f.request.stage,
            "request_digest": "same-request",
        }
        with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            f.checkpoint.claim_repair("summary:1", "same-request")
    with retrying(f):
        with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            claim(f)
    assert all(path.read_bytes() == before for path, before in retained.items())


@pytest.mark.parametrize(
    "change",
    [
        {"observation": "transport"},
        {"state": "completed", "observation": "transport", "ended_at": "2026-10-12T00:01:00+00:00"},
        {"state": "failed"},
        {"state": "cancelled"},
        {"send_status": "not_sent"},
        {"send_status": "unknown"},
        {"ended_at": "2026-10-12T00:01:00+00:00"},
        {"business_state": "failed"},
        {"provider_request_id": "already-received-response"},
        {"logical_call_id": None},
        {"policy_fingerprint": "wrong-policy"},
        {"model": "openai/other-model"},
    ],
)
def test_observed_or_unbound_request_never_authorizes_recovery(interrupted_summary, change):
    f = interrupted_summary
    value = f.request.model_dump(mode="json") | change
    write(f.request_path, UsageRequest.model_validate(value).model_dump(mode="json"))
    before = f.checkpoint.path.read_bytes()
    with retrying(f), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        claim(f)
    assert f.checkpoint.path.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["source_id", "source_revision_id", "unit_id", "unit_revision_id"]
)
def test_another_source_or_processing_revision_cannot_claim_retained_repair(
    interrupted_summary, field
):
    f = interrupted_summary
    f.scope = replace(f.scope, **{field: "9" * 32})
    with retrying(f), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        claim(f)


@pytest.mark.parametrize(
    "change",
    [{"state": "accepted"}, {"failure_code": "page_literal_conflict"}, {"recovery_used": True}],
)
def test_checkpoint_outcome_is_not_reinterpreted_as_unknown(interrupted_summary, change):
    f = interrupted_summary
    manifest = json.loads(f.checkpoint.path.read_text())
    manifest["entries"]["summary:1"].update(change)
    write(f.checkpoint.path, manifest)
    before = f.checkpoint.path.read_bytes()
    with retrying(f), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        claim(f)
    assert f.checkpoint.path.read_bytes() == before


def test_recovery_cannot_take_another_summary_stages_unknown_request(interrupted_summary):
    f = interrupted_summary
    with retrying(f), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        f.checkpoint.claim_generation(
            "summary:1", "same-request", stage="compile.document-summary:2"
        )


@pytest.mark.parametrize("change", ["digest", "conditions", "key"])
def test_changed_request_does_not_claim_old_unknown_recovery(interrupted_summary, change):
    f = interrupted_summary
    before = f.checkpoint.path.read_bytes()
    checkpoint, key, signature = f.checkpoint, "summary:1", "same-request"
    if change == "conditions":
        checkpoint = CompileCheckpoints(f.root, f.task, {"document": "manual"}, {"protocol": "new"})
    elif change == "key":
        key = "summary:2"
    else:
        signature = "another-request"
    # A distinct request may generate normally; it must not consume or rewrite the
    # original request's interrupted-repair allowance as if it were a recovery.
    with retrying(f):
        checkpoint.claim_generation(key, signature, stage="compile.document-summary:2")
    assert f.checkpoint.path.read_bytes() == before
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        f.checkpoint.require_generation("summary:1", "same-request")


@pytest.mark.parametrize("latest_state", ["completed", "tied_unknown"])
def test_only_unique_latest_unknown_can_authorize_recovery(interrupted_summary, latest_state):
    f = interrupted_summary
    value = f.request.model_dump(mode="json") | {"request_attempt_id": "5" * 32}
    if latest_state == "completed":
        value.update(
            started_at="2026-10-12T00:01:00+00:00", state="completed", observation="transport"
        )
    path = f.request_path.parent / (value["request_attempt_id"] + ".json")
    write(path, UsageRequest.model_validate(value).model_dump(mode="json"))
    with retrying(f), pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        claim(f)


def test_saved_recovery_keeps_marker_and_restores_cache_without_ledger_edit(interrupted_summary):
    f = interrupted_summary
    old = f.request_path.read_bytes()
    with retrying(f):
        claim(f)
        f.checkpoint.save("summary:1", "same-request", {"content": "Accepted source fact."})
    entry = json.loads(f.checkpoint.path.read_text())["entries"]["summary:1"]
    assert entry["state"] == "accepted"
    assert entry["repair_used"] is True and entry["recovery_used"] is True
    assert f.checkpoint.load("summary:1", "same-request") == {"content": "Accepted source fact."}
    assert f.request_path.read_bytes() == old
