"""Timing comes from actual native stages and loopback model transport."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_execution
from ultrakb.config import LlmCredentialBundle
from ultrakb.execution_timing import phase_observation
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings


def execution(service, **credential_options):
    return ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings(
                "openai/gpt-4o-mini",
                LlmCredentialBundle(
                    api_key="offline-test-key",
                    base_url=service.url,
                    **credential_options,
                ),
            ),
            ModelCallPolicy(concurrency=1, retries=0),
        )
    )


def detail(root, context):
    return read_knowledge_execution(root, context.storage_execution.execution_id, limit=128)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_transport_observations_keep_queue_wait_separate_from_sent_request(kb_dir, asynchronous):
    gate = threading.Event()
    with ErrorTransport(status=400, gate=gate) as service:
        context = execution(service)
        with context.begin(kb_dir):

            def invoke(work):
                request = ModelRequest(
                    [{"role": "user", "content": "Local error only."}],
                    operation="compile",
                    stage=f"document-summary:{work}",
                    generation_options={"max_tokens": 16},
                )
                with pytest.raises(litellm.BadRequestError):
                    if asynchronous:
                        asyncio.run(context.executor.acomplete("compile", request))
                    else:
                        context.executor.complete("compile", request)

            with ThreadPoolExecutor(max_workers=2) as workers:
                try:
                    first = workers.submit(copy_context().run, invoke, "1")
                    assert service.wait_for_requests(1)
                    second = workers.submit(copy_context().run, invoke, "2")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        queued = [
                            s
                            for s in detail(kb_dir, context).stages
                            if s.stage_code == "model.queue.v1"
                        ]
                        if len(queued) == 2:
                            break
                        time.sleep(0.01)
                    assert len(queued) == 2 and len(service.requests) == 1
                    assert sorted(s.state for s in queued) == ["completed", "started"]
                finally:
                    gate.set()
                first.result(timeout=5)
                second.result(timeout=5)
    result = detail(kb_dir, context)
    queues = [s for s in result.stages if s.stage_code == "model.queue.v1"]
    slots = [s for s in result.stages if s.stage_code == "model.slot.v1"]
    requests = [s for s in result.stages if s.stage_code == "model_request"]
    assert len(queues) == len(slots) == len(requests) == len(result.requests) == 2
    assert all(s.state == "completed" and s.duration_seconds >= 0 for s in queues)
    assert all(s.state == "failed" and s.duration_seconds > 0 for s in slots + requests)
    assert result.execution_usage.actual_sends == 2
    assert all(r.state == "failed" and r.send_status == "sent" for r in result.requests)
    assert "offline-test-key" not in result.model_dump_json()


def test_phase_duration_uses_one_process_clock_despite_wall_clock_adjustment(kb_dir, monkeypatch):
    import ultrakb.storage.execution_store as clocks
    from ultrakb.application.views import published_scope
    from ultrakb.storage.execution_store import ExecutionStore

    store = ExecutionStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    run = store.begin("clock-boundary", fingerprint="a" * 64, max_calls=0)
    with monkeypatch.context() as patch:
        patch.setattr(clocks.time, "monotonic", lambda: 10.0)
        patch.setattr(clocks, "now", lambda: "2026-10-18T12:00:00+00:00")
        stage = store.start_stage(run.execution_id, "compiler.validation.v1")
        patch.setattr(clocks.time, "monotonic", lambda: 13.0)
        patch.setattr(clocks, "now", lambda: "2026-10-18T11:00:00+00:00")
        store.finish_stage(stage, state="completed")
    result = read_knowledge_execution(kb_dir, run.execution_id)
    assert result.stages[0].duration_seconds == 3
    store.finish(run.execution_id, state="completed", work_settled=True)


def test_execution_without_stage_observations_does_not_invent_phase_times(kb_dir):
    from ultrakb.application.views import published_scope
    from ultrakb.storage.execution_store import ExecutionStore

    store = ExecutionStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    run = store.begin("unobserved", fingerprint="a" * 64, max_calls=0)
    result = read_knowledge_execution(kb_dir, run.execution_id)
    assert result.stages == result.requests == ()
    assert result.execution.duration_seconds is None
    store.finish(run.execution_id, state="completed", work_settled=True)


def test_transport_timeout_settles_without_inventing_a_response(kb_dir):
    with ErrorTransport(status=504, gate=threading.Event()) as service:
        context = execution(service, timeout=0.05)
        with context.begin(kb_dir), pytest.raises(litellm.Timeout):
            context.executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Local timeout."}],
                    operation="compile",
                    stage="summary",
                ),
            )
    result = detail(kb_dir, context)
    [request] = result.requests
    assert request.state == "failed" and request.usage_status == "unknown"
    assert request.send_status == "sent"
    [stage] = [s for s in result.stages if s.stage_code == "model_request"]
    assert stage.state == "failed" and stage.duration_seconds > 0
    assert result.execution_usage.actual_sends == 1


def test_phase_observation_keeps_task_and_failed_validation_outcome(kb_dir):
    context = ExecutionContext(usage_task_id="a" * 32)
    with context.begin(kb_dir):
        with pytest.raises(ValueError, match="Invalid accepted result"):
            with phase_observation("validation", stage="compile.summary", work_id="window:1"):
                raise ValueError("Invalid accepted result")
    result = detail(kb_dir, context)
    [stage] = [s for s in result.stages if s.stage_code == "compiler.validation.v1"]
    assert stage.state == "failed" and stage.duration_seconds is not None
    assert stage.work_key == "window:1" and stage.error_code == "ValueError"
    assert result.execution.application_task_id == "a" * 32


def test_observation_storage_failure_preserves_the_business_error(kb_dir, monkeypatch):
    from ultrakb.storage.execution_store import ExecutionStore

    original = ExecutionStore.start_stage

    def unavailable(self, execution_id, code, **options):
        if code == "compiler.validation.v1":
            raise OSError("Observation storage unavailable")
        return original(self, execution_id, code, **options)

    monkeypatch.setattr(ExecutionStore, "start_stage", unavailable)
    context = ExecutionContext()
    with context.begin(kb_dir):
        with pytest.raises(ValueError, match="Paid result validation failed"):
            with phase_observation("validation", stage="compile.summary"):
                raise ValueError("Paid result validation failed")
    assert not any(s.stage_code == "compiler.validation.v1" for s in detail(kb_dir, context).stages)


def test_late_transport_receipt_cannot_write_into_a_recreated_knowledge_base(kb_dir):
    import httpx
    from native_usage_fixtures import native_usage_execution, read_requests

    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.kb_admin import delete_kb
    from ultrakb.lifecycle import KnowledgeBaseRemoved, current_generation
    from ultrakb.llm_usage import begin_request
    from ultrakb.llm_usage_transport import observe_model_call

    progress = []
    with (
        ErrorTransport(status=400) as service,
        native_usage_execution(
            kb_dir,
            "fixture",
            on_event=progress.append,
        ) as scope,
    ):
        with observe_model_call("openai/gpt-4o-mini", "compile.summary") as call:
            response = httpx.post(service.url + "/chat/completions", json={"messages": []})
            [request_id] = call.sent_ids
        with observe_model_call("openai/gpt-4o-mini", "compile.summary") as unsent:
            pass
        assert read_requests(kb_dir)
    delete_kb(kb_dir)
    initialize_kb(kb_dir, seed_environment=False)
    assert current_generation(kb_dir) != scope.generation
    progress.clear()
    with pytest.raises((KnowledgeBaseRemoved, ValueError, RuntimeError)):
        call.received(request_id, response)
    with pytest.raises((KnowledgeBaseRemoved, ValueError, RuntimeError)):
        begin_request("openai/gpt-4o-mini", "compile.summary", scope=scope)
    with pytest.raises((KnowledgeBaseRemoved, ValueError, RuntimeError)):
        unsent.sending()
    assert not read_requests(kb_dir) and not progress
