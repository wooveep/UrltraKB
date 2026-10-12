"""Strict local ledger/checkpoint fixtures; no model or product application call."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from openkb import compile_recovery
from openkb.agent.page_response import PageResponseError
from openkb.compile_checkpoints import CompileCheckpoints
from openkb.lifecycle import current_generation
from openkb.llm_usage import _SCOPE, UsageRequest, UsageScope
from openkb.llm_usage_models import UsageExecution
from openkb.locks import LockCancelled, atomic_write_json
from openkb.model_task import ModelTask

TASK = "1" * 32
SOURCE = "2" * 32
REVISION = "3" * 32
UNIT = "4" * 32
UNIT_REVISION = "5" * 32
OLD = "6" * 32
CURRENT = "7" * 32
REQUEST = "8" * 32
STAGE = "compile.document-summary:5"
DIGEST = "a" * 64


@contextmanager
def scoped(scope):
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def setup(tmp_path):
    root = tmp_path.resolve()
    root.mkdir(exist_ok=True)
    task = ModelTask(
        task_id=TASK,
        kb_generation=current_generation(root),
        fingerprint="fixture-fingerprint",
        bindings={"models": {"compile": "fixture/model"}},
        policy={"max_calls": 8},
        used=2,
        stages={"compile": 2},
        inputs={SOURCE: REVISION},
    )
    atomic_write_json(root / ".openkb/model-tasks" / (TASK + ".json"), task.model_dump(mode="json"))
    for identity, operation in ((OLD, "import_document"), (CURRENT, "retry_source")):
        ex = UsageExecution(
            execution_id=identity,
            operation=operation,
            state="started",
            model_task_id=TASK,
            source_ids=[SOURCE],
        )
        atomic_write_json(
            root / ".openkb/usage/executions" / (identity + ".json"), ex.model_dump(mode="json")
        )
    usage = UsageRequest(
        request_attempt_id=REQUEST,
        logical_call_id="9" * 32,
        operation="compile",
        policy_fingerprint=task.fingerprint,
        execution_id=OLD,
        source_id=SOURCE,
        source_revision_id=REVISION,
        unit_id=UNIT,
        unit_revision_id=UNIT_REVISION,
        model="fixture/model",
        stage=STAGE,
        started_at="2026-10-12T08:00:00+00:00",
        state="started",
        send_status="sent",
        observation="call",
    )
    atomic_write_json(
        root / ".openkb/usage/requests" / (REQUEST + ".json"), usage.model_dump(mode="json")
    )
    checkpoint = CompileCheckpoints(root, task, {"document": "fixture"}, {"protocol": "unchanged"})
    checkpoint.claim_repair("summary:5", DIGEST)
    scope = UsageScope(root, CURRENT, SOURCE, REVISION, UNIT, UNIT_REVISION)
    return root, checkpoint, scope


def ledger(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for area in (".openkb/usage", ".openkb/model-tasks")
        for p in (root / area).rglob("*.json")
    }


def claim(checkpoint):
    return checkpoint.claim_generation("summary:5", DIGEST, stage=STAGE)


def test_matching_recovery_preserves_old_ledger_bytes(tmp_path):
    root, checkpoint, scope = setup(tmp_path)
    before = ledger(root)
    with scoped(scope), compile_recovery.explicit_compile_retry(root, SOURCE):
        claim(checkpoint)
    entry = json.loads(checkpoint.path.read_text())["entries"]["summary:5"]
    assert entry["state"] == "resuming" and entry["recovery_used"] is True
    assert entry["recovery"] == {
        "request_attempt_id": REQUEST,
        "logical_call_id": "9" * 32,
        "execution_id": OLD,
        "stage": STAGE,
        "request_digest": DIGEST,
    }
    assert ledger(root) == before


@pytest.mark.parametrize("sources", [[], ["b" * 32]])
def test_historical_execution_must_own_request_source(tmp_path, sources):
    root, checkpoint, scope = setup(tmp_path)
    path = root / ".openkb/usage/executions" / (OLD + ".json")
    record = UsageExecution.model_validate_json(path.read_text())
    atomic_write_json(
        path, record.model_copy(update={"source_ids": sources}).model_dump(mode="json")
    )
    before = checkpoint.path.read_bytes()
    with scoped(scope), compile_recovery.explicit_compile_retry(root, SOURCE):
        with pytest.raises((PageResponseError, ValueError)):
            claim(checkpoint)
    assert checkpoint.path.read_bytes() == before


@pytest.mark.parametrize("wrong_scope", ["absent", "source", "root"])
def test_explicit_retry_scope_cannot_cross_source_or_kb(tmp_path, wrong_scope):
    root, checkpoint, scope = setup(tmp_path)
    before = checkpoint.path.read_bytes()
    with scoped(scope):
        if wrong_scope == "absent":
            with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
                claim(checkpoint)
        else:
            chosen_root = root / "other" if wrong_scope == "root" else root
            chosen_source = "b" * 32 if wrong_scope == "source" else SOURCE
            with compile_recovery.explicit_compile_retry(chosen_root, chosen_source):
                with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
                    claim(checkpoint)
    assert checkpoint.path.read_bytes() == before


def test_explicit_retry_context_resets_on_nested_exception(tmp_path):
    root, _, scope = setup(tmp_path)
    with scoped(scope):
        assert compile_recovery.interrupted_repair_request(root, TASK, STAGE) is None
        with compile_recovery.explicit_compile_retry(root, SOURCE):
            assert compile_recovery.interrupted_repair_request(root, TASK, STAGE) is not None
            with pytest.raises(RuntimeError, match="fixture"):
                with compile_recovery.explicit_compile_retry(root, "b" * 32):
                    assert compile_recovery.interrupted_repair_request(root, TASK, STAGE) is None
                    raise RuntimeError("fixture")
            assert compile_recovery.interrupted_repair_request(root, TASK, STAGE) is not None
        assert compile_recovery.interrupted_repair_request(root, TASK, STAGE) is None


def test_concurrent_claimants_consume_at_most_one_recovery(tmp_path):
    root, checkpoint, scope = setup(tmp_path)
    before = ledger(root)
    barrier = threading.Barrier(2)

    def contender():
        with scoped(scope), compile_recovery.explicit_compile_retry(root, SOURCE):
            barrier.wait(timeout=3)
            try:
                claim(checkpoint)
                return "claimed"
            except PageResponseError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: contender(), range(2)))
    assert sorted(outcomes) == ["claimed", "compile_repair_exhausted"]
    assert ledger(root) == before


@pytest.mark.parametrize("moment", ["before", "during_ledger_scan"])
def test_cancellation_does_not_consume_recovery_before_a_send(tmp_path, monkeypatch, moment):
    root, checkpoint, scope = setup(tmp_path)
    before = checkpoint.path.read_bytes()
    cancelled = moment == "before"

    def check():
        if cancelled:
            raise LockCancelled("fixture cancelled before any send")

    read = compile_recovery.read_requests

    def read_then_cancel(kb):
        nonlocal cancelled
        records = read(kb)
        cancelled = True
        return records

    monkeypatch.setattr("openkb.llm_execution.check_model_stop", check)
    if moment == "during_ledger_scan":
        monkeypatch.setattr(compile_recovery, "read_requests", read_then_cancel)
    with scoped(scope), compile_recovery.explicit_compile_retry(root, SOURCE):
        with pytest.raises(LockCancelled):
            claim(checkpoint)
    assert checkpoint.path.read_bytes() == before


def test_completed_execution_cannot_authorize_unknown_recovery(tmp_path):
    root, checkpoint, scope = setup(tmp_path)
    path = root / ".openkb/usage/executions" / (OLD + ".json")
    record = UsageExecution.model_validate_json(path.read_text())
    atomic_write_json(
        path, record.model_copy(update={"state": "completed"}).model_dump(mode="json")
    )
    before = checkpoint.path.read_bytes()
    with scoped(scope), compile_recovery.explicit_compile_retry(root, SOURCE):
        with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            claim(checkpoint)
    assert checkpoint.path.read_bytes() == before
