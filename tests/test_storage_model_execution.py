"""Real SDK transport and durable application execution share the new main store."""

import json

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.application.execution import ExecutionContext
from ultrakb.config import save_config
from ultrakb.llm_execution import ModelRequest
from ultrakb.storage.database import create_database
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.receipt_store import ReceiptStore
from ultrakb.storage.usage_store import UsageStore


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_sdk_error_response_and_sent_usage_survive_the_application_failure(tmp_path, asynchronous):
    import asyncio

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"max_calls": 4, "retries": 0}},
    )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with pytest.raises(litellm.BadRequestError), context.begin(root):
            request = ModelRequest(
                [{"role": "user", "content": "Only return a local HTTP error."}],
                operation="compile",
                stage="summary",
            )
            if asynchronous:
                asyncio.run(context.executor.acomplete("compile", request))
            else:
                context.executor.complete("compile", request)
        assert len(service.requests) == 1
    execution = context.storage_execution
    usage = UsageStore(root, identity)
    [request] = usage.requests(execution_id=execution.execution_id).records
    assert request.state == "failed"
    assert request.send_status == "sent"
    assert request.input_total is None and request.output_total is None
    assert request.observation == "transport"
    assert json.loads(ReceiptStore(root, identity).read(request.request_attempt_id)) == service.body
    assert usage.summary(execution_id=execution.execution_id)["actual_sends"] == 1
    assert ExecutionStore(root, identity).status(execution.execution_id) == "failed"
    assert ExecutionStore(root, identity).budget(execution.model_budget_id).used == 1
    assert not (root / ".ultrakb/model-tasks").exists()
    assert not (root / ".ultrakb/usage").exists()
    assert not (root / ".ultrakb/observations").exists()


def test_known_unsent_attempt_releases_budget_before_the_next_actual_send(tmp_path):
    from ultrakb.llm_usage_transport import observe_model_call
    from ultrakb.locks import LockCancelled

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"max_calls": 1, "retries": 0}},
    )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with pytest.raises(litellm.BadRequestError), context.begin(root):
            with pytest.raises(LockCancelled):
                with observe_model_call("openai/gpt-4o-mini", "compile.summary"):
                    raise LockCancelled("Stopped before transport")
            assert (
                ExecutionStore(root, identity)
                .budget(context.storage_execution.model_budget_id)
                .used
                == 0
            )
            context.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Only return a local HTTP error."}],
                    operation="compile",
                    stage="summary",
                ),
            )
        assert len(service.requests) == 1
    requests = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert [(item.send_status, item.state) for item in requests] == [
        ("not_sent", "cancelled"),
        ("sent", "failed"),
    ]
    assert (
        ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used == 1
    )


def test_actual_sdk_wait_keeps_receipts_durable_and_allows_unrelated_gc_and_settings(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ultrakb.application.execution_configuration import patch_storage_config
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "language": "en", "model_policy": {"retries": 0}},
    )
    with storage_stage(root, identity) as db:
        put_blob(db, b"unrelated orphan during the SDK wait")
    gate = Event()
    with ErrorTransport(status=400, gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)

        def invoke():
            with pytest.raises(litellm.BadRequestError), context.begin(root):
                context.executor.complete(
                    "compile",
                    ModelRequest(
                        [{"role": "user", "content": "Only return a local HTTP error."}],
                        operation="compile",
                        stage="summary",
                    ),
                )
            return context.snapshot.values()["effective"]["language"]

        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(invoke)
            try:
                assert service.wait_for_requests(1)
                execution = context.storage_execution
                [started] = (
                    UsageStore(root, identity).requests(execution_id=execution.execution_id).records
                )
                assert (started.state, started.send_status) == ("started", "sent")
                assert ExecutionStore(root, identity).status(execution.execution_id) == "started"
                collected = Maintenance(root, identity).collect()
                assert collected.status != "busy" and collected.deleted_count == 1
                patch_storage_config(
                    root, identity, KbConfigPatchRequest(kb=str(root), config={"language": "zh"})
                )
            finally:
                gate.set()
            assert pending.result(timeout=5) == "en"
    assert (
        UsageStore(root, identity).summary(execution_id=execution.execution_id)["actual_sends"] == 1
    )


def test_received_response_sql_failure_retains_repair_receipt_without_a_transport_retry(tmp_path):
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.storage.coordination import storage_stage

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"max_calls": 5, "retries": 2}},
    )
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_response BEFORE INSERT ON ukb_response_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected received response failure'); END"
        )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with pytest.raises(RecoveryRequired, match="retained"), context.begin(root):
            context.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Only return a local HTTP error."}],
                    operation="compile",
                    stage="summary",
                ),
            )
        assert len(service.requests) == 1
    assert list((root / ".ultrakb/receipts/pending").glob("*.json"))
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_response")
    assert ReceiptStore(root, identity).repair().repaired == 1
    [request] = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert (request.state, request.send_status, request.observation) == (
        "failed",
        "sent",
        "transport",
    )
    assert json.loads(ReceiptStore(root, identity).read(request.request_attempt_id)) == service.body
    summary = UsageStore(root, identity).summary(
        execution_id=context.storage_execution.execution_id
    )
    assert summary["actual_sends"] == 1 and summary["collection_complete"]


def test_usage_write_failure_before_transport_blocks_resend_and_repairs_known_unsent_budget(
    tmp_path,
):
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.storage.coordination import storage_stage

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"max_calls": 5, "retries": 2}},
    )
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_usage BEFORE UPDATE ON ukb_usage_requests "
            "BEGIN SELECT RAISE(ABORT, 'injected usage receipt failure'); END"
        )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with pytest.raises(RecoveryRequired, match="receipt"), context.begin(root):
            context.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Only return a local HTTP error."}],
                    operation="compile",
                    stage="summary",
                ),
            )
        assert service.requests == []
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_usage")
    repaired = ReceiptStore(root, identity).repair()
    assert repaired.invalid == 0 and repaired.repaired >= 1
    [request] = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert (request.state, request.send_status) == ("failed", "not_sent")
    assert (
        ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used == 0
    )
    with pytest.raises(KeyError):
        ReceiptStore(root, identity).read(request.request_attempt_id)


@pytest.mark.parametrize("order", ["cancel_first", "cancel_last"])
def test_late_completed_callback_cannot_revive_a_cancelled_business_result(tmp_path, order):
    from ultrakb.llm_usage import active_scope, finish_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"retries": 0}},
    )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with context.begin(root):
            scope = active_scope()
            with pytest.raises(litellm.BadRequestError):
                context.executor.complete(
                    "compile",
                    ModelRequest(
                        [{"role": "user", "content": "Only return a local HTTP error."}],
                        operation="compile",
                        stage="summary",
                    ),
                )
            [record] = UsageStore(root, identity).requests(execution_id=scope.execution_id).records
            if order == "cancel_first":
                finish_request(
                    record.request_attempt_id,
                    scope=scope,
                    state="cancelled",
                    business_state="cancelled",
                )
                finish_request(
                    record.request_attempt_id,
                    scope=scope,
                    state="completed",
                    business_state="completed",
                )
            else:
                UsageStore(root, identity).merge(
                    record.request_attempt_id,
                    version=100,
                    business_state="completed",
                )
                ReceiptStore(root, identity).capture_usage(
                    record.request_attempt_id,
                    version=99,
                    usage={"business_state": "cancelled"},
                )
    current = UsageStore(root, identity).request(record.request_attempt_id)
    assert current.send_status == "sent" and current.business_state == "cancelled"
    assert (
        ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used == 1
    )


@pytest.mark.asyncio
async def test_cancelled_sdk_wait_retains_the_late_transport_response(tmp_path):
    import asyncio
    from threading import Event

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"retries": 0}},
    )
    gate = Event()
    with ErrorTransport(status=400, gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)

        async def invoke():
            with context.begin(root):
                await context.executor.acomplete(
                    "compile",
                    ModelRequest(
                        [{"role": "user", "content": "Only return a local HTTP error."}],
                        operation="compile",
                        stage="summary",
                    ),
                )

        pending = asyncio.create_task(invoke())
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            pending.cancel()
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(service.requests) == 1
    execution = context.storage_execution
    [request] = UsageStore(root, identity).requests(execution_id=execution.execution_id).records
    assert request.send_status == "sent" and request.business_state == "cancelled"
    assert json.loads(ReceiptStore(root, identity).read(request.request_attempt_id)) == service.body
    assert ExecutionStore(root, identity).status(execution.execution_id) == "cancelled"


def test_a_callback_cannot_attach_another_executions_response_to_its_scope(tmp_path):
    from ultrakb.llm_usage import active_scope, finish_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"retries": 0}},
    )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = ExecutionContext(storage_identity=identity)
        with pytest.raises(litellm.BadRequestError), first.begin(root):
            first.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Only return a local HTTP error."}],
                    operation="compile",
                    stage="summary",
                ),
            )
        usage = UsageStore(root, identity)
        [original] = usage.requests(execution_id=first.storage_execution.execution_id).records
        second = ExecutionContext(storage_identity=identity)
        with pytest.raises(ValueError, match="execution"), second.begin(root):
            finish_request(
                original.request_attempt_id,
                {
                    "id": "foreign-response",
                    "usage": {"prompt_tokens": 999, "completion_tokens": 999},
                },
                scope=active_scope(),
            )
    assert usage.request(original.request_attempt_id) == original


@pytest.mark.parametrize("cache_hit", [False, True], ids=["unobserved", "sdk_cache"])
def test_sdk_result_without_transport_does_not_invent_a_send_or_a_cache_charge(
    tmp_path, monkeypatch, cache_hit
):
    import httpx

    from ultrakb.llm_usage import active_scope, finish_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    response = litellm.ModelResponse(
        id="sdk-result",
        model="gpt-4o-mini",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "retained result"},
                "finish_reason": "stop",
            }
        ],
    )
    if cache_hit:
        response._hidden_params["cache_hit"] = True

    def no_transport(*args, **kwargs):
        raise AssertionError("No network request belongs to this provider-boundary result")

    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response)
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        scope = active_scope()
        monkeypatch.setattr(httpx.Client, "send", no_transport)
        result = context.executor.complete(
            "compile",
            ModelRequest(
                [{"role": "user", "content": "Read the supplied provider result."}],
                operation="compile",
                stage="summary",
            ),
        )
    assert result.text == "retained result" and context.executor.calls == 0
    usage = UsageStore(root, identity)
    [request] = usage.requests(execution_id=context.storage_execution.execution_id).records
    assert request.send_status == ("not_sent" if cache_hit else "unknown")
    assert request.application_cache_hit is cache_hit
    assert request.input_total == (0 if cache_hit else None)
    assert usage.summary(execution_id=context.storage_execution.execution_id)["actual_sends"] == 0
    assert ExecutionStore(root, identity).budget(
        context.storage_execution.model_budget_id
    ).used == (0 if cache_hit else 1)
    retained = json.loads(ReceiptStore(root, identity).read(request.request_attempt_id))
    assert retained["choices"][0]["message"]["content"] == "retained result"
    finish_request(
        request.request_attempt_id,
        {"usage": {"prompt_tokens": 500, "completion_tokens": 80}},
        observation="call",
        scope=scope,
    )
    updated = usage.request(request.request_attempt_id)
    assert updated.input_total == (0 if cache_hit else None)
    assert updated.output_total == (0 if cache_hit else None)


def test_reentered_request_initialization_keeps_one_intent_and_original_stage(tmp_path):
    from ultrakb.llm_usage import active_scope, begin_request, finish_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        scope = active_scope()
        request_id = begin_request("openai/gpt-4o-mini", "compile.summary", send_status="not_sent")
        original = UsageStore(root, identity).request(request_id)
        scope.storage.begin(original)
        finish_request(request_id, scope=scope, state="failed")
        scope.storage.begin(original)
        with pytest.raises(ValueError, match="identity"):
            scope.storage.begin(original.model_copy(update={"model": "another/model"}))
    [request] = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert (request.state, request.send_status) == ("failed", "not_sent")
    assert (
        ExecutionStore(root, identity).status(context.storage_execution.execution_id) == "completed"
    )
    assert (
        ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used == 0
    )


def test_failed_stage_observation_does_not_hide_the_model_error_or_leave_execution_running(
    tmp_path,
):
    from ultrakb.storage.coordination import storage_stage

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {"model": "openai/gpt-4o-mini", "model_policy": {"retries": 0}},
    )
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_stage BEFORE UPDATE ON ukb_stage_attempts "
            "BEGIN SELECT RAISE(ABORT, 'injected stage observation failure'); END"
        )
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(storage_identity=identity)
        with pytest.raises(litellm.BadRequestError), context.begin(root):
            context.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Only return a local HTTP error."}],
                    operation="compile",
                    stage="summary",
                ),
            )
        assert len(service.requests) == 1
    first = context.storage_execution
    assert ExecutionStore(root, identity).status(first.execution_id) == "failed"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_stage")
    with context.begin(root):
        assert context.storage_execution.task_id == first.task_id
        assert context.storage_execution.model_budget_id == first.model_budget_id
    assert ExecutionStore(root, identity).budget(first.model_budget_id).used == 1
    [request] = UsageStore(root, identity).requests(execution_id=first.execution_id).records
    assert request.send_status == "sent"
    assert json.loads(ReceiptStore(root, identity).read(request.request_attempt_id)) == service.body


def test_stage_cleanup_and_a_late_receipt_serialize_without_losing_the_execution(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ultrakb.llm_usage import active_scope, begin_request, finish_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    entered, resume, finished = Event(), Event(), Event()
    original_finish = ExecutionStore.finish_stage

    def pause_cleanup(self, stage_id, *, state, **kwargs):
        if state == "failed":
            entered.set()
            assert resume.wait(5)
        return original_finish(self, stage_id, state=state, **kwargs)

    monkeypatch.setattr(ExecutionStore, "finish_stage", pause_cleanup)
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        scope = active_scope()
        request_id = begin_request("openai/gpt-4o-mini", "compile.summary", send_status="not_sent")

        def late_receipt():
            try:
                finish_request(request_id, scope=scope, state="failed")
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=2) as workers:
            cleanup = workers.submit(scope.storage.close)
            assert entered.wait(5)
            callback = workers.submit(late_receipt)
            try:
                finished.wait(0.25)
            finally:
                resume.set()
            cleanup.result(timeout=5)
            callback.result(timeout=5)
    assert (
        ExecutionStore(root, identity).status(context.storage_execution.execution_id) == "completed"
    )
    assert UsageStore(root, identity).request(request_id).state == "failed"


def test_finished_execution_releases_an_unsent_intent_and_revokes_its_send_capability(tmp_path):
    from ultrakb.llm_usage_transport import observe_model_call

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        with observe_model_call("openai/gpt-4o-mini", "compile.summary") as admitted:
            pass
    execution = context.storage_execution
    assert ExecutionStore(root, identity).budget(execution.model_budget_id).used == 0
    with pytest.raises(RuntimeError, match="finished"):
        admitted.sending()
    request = UsageStore(root, identity).request(admitted.first_id)
    assert (request.state, request.send_status) == ("failed", "not_sent")


def test_out_of_order_cache_proof_releases_budget_and_actual_transport_can_supersede_it(tmp_path):
    from ultrakb.llm_usage import active_scope, begin_request

    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(root / ".ultrakb/config.yaml", {"model": "openai/gpt-4o-mini"})
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        scope = active_scope()
        request_id = begin_request("openai/gpt-4o-mini", "compile.summary", send_status="not_sent")
        usage = UsageStore(root, identity)
        usage.merge(
            request_id,
            version=100,
            state="completed",
            send_status="unknown",
            input_total=500,
            output_total=80,
        )
        ReceiptStore(root, identity).capture_usage(
            request_id,
            version=1,
            usage={
                "state": "completed",
                "send_status": "not_sent",
                "application_cache_hit": True,
                "input_total": 0,
                "output_total": 0,
                "cached_input": 0,
                "reasoning_output": 0,
            },
        )
        cached = usage.request(request_id)
        assert cached.send_status == "not_sent" and cached.input_total == cached.output_total == 0
        assert cached.usage_status == "known"
        assert (
            ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used
            == 0
        )
        scope.storage.finish(
            request_id,
            {"usage": {"prompt_tokens": 30, "completion_tokens": 7}},
            observation="transport",
            state="completed",
            terminal=True,
            send_status=None,
            business_state="completed",
        )
    sent = usage.request(request_id)
    assert sent.send_status == "sent" and sent.input_total == 30 and sent.output_total == 7
    assert sent.cached_input is None and sent.reasoning_output is None
    assert (
        ExecutionStore(root, identity).budget(context.storage_execution.model_budget_id).used == 1
    )
    usage.merge(
        request_id,
        version=200,
        application_cache_hit=True,
        send_status="not_sent",
        input_total=0,
        output_total=0,
        cached_input=0,
        reasoning_output=0,
    )
    sent = usage.request(request_id)
    assert sent.send_status == "sent" and sent.input_total == 30 and sent.output_total == 7
    assert sent.cached_input is None and sent.reasoning_output is None
