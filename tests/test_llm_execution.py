"""Task policies are enforced at the actual SDK transport boundary."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def executor_for(service, **policy):
    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    return CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini", LlmCredentialBundle(api_key="fixture", base_url=service.url)
        ),
        ModelCallPolicy(**policy),
    )


def request(**values):
    from openkb.llm_execution import ModelRequest

    return ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary", **values
    )


def test_retry_attempts_share_logical_call_and_do_not_double_bill(model_service, kb_dir):
    from openkb.llm_usage import aggregate_usage, read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.extend([(429, {"error": {"message": "busy"}}), (200, response())])
    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service, max_calls=2, retries=1, backoff=0)
        result = executor.complete("compile", request(parent_call_id="a" * 32))
    records = read_requests(kb_dir)
    assert result.text == "ok" and result.raw_usage_available
    assert len(model_service.requests) == len(records) == 2
    assert len({r.logical_call_id for r in records}) == 1
    assert all(r.parent_call_id == "a" * 32 and r.operation == "compile" for r in records)
    assert aggregate_usage(kb_dir)["input_total"] == 12


@pytest.mark.asyncio
async def test_parallel_calls_cannot_overspend_budget(model_service):
    from openkb.llm_execution import ModelBudgetExceeded

    model_service.replies.extend([(200, response())] * 4)
    executor = executor_for(model_service, max_calls=2, concurrency=4)
    results = await asyncio.gather(
        *(executor.acomplete("compile", request()) for _ in range(4)), return_exceptions=True
    )
    assert len(model_service.requests) == 2
    assert sum(isinstance(r, ModelBudgetExceeded) for r in results) == 2


def test_algorithm_cannot_override_transport_binding():
    with pytest.raises(ValueError, match="generation"):
        request(generation_options={"api_key": "injected"})


def test_compiler_uses_the_active_budget_for_repair_calls(model_service):
    from openkb.agent.compiler import _llm_call
    from openkb.llm_execution import ModelBudgetExceeded

    model_service.replies.append((200, response()))
    executor = executor_for(model_service, max_calls=1)
    with executor.activate():
        assert (
            _llm_call("ignored-by-frozen-binding", [{"role": "user", "content": "hi"}], "summary")
            == "ok"
        )
        with pytest.raises(ModelBudgetExceeded):
            _llm_call(
                "ignored-by-frozen-binding", [{"role": "user", "content": "repair"}], "repair"
            )
    assert len(model_service.requests) == 1


@pytest.mark.parametrize("status", [401, 400, 500, 503])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_retry_policy_counts_actual_sends(model_service, status, asynchronous):
    import litellm

    model_service.replies.extend([(status, {"error": {"message": "fixture"}})] * 4)
    executor = executor_for(model_service, max_calls=4, retries=1, backoff=0)
    with pytest.raises(Exception) as caught:
        if asynchronous:
            await executor.acomplete("compile", request())
        else:
            executor.complete("compile", request())
    if status == 401:
        assert isinstance(caught.value, litellm.AuthenticationError)
    assert len(model_service.requests) == (1 if status < 500 else 2)


def test_late_sync_result_is_recorded_but_never_published(model_service, kb_dir):
    from openkb.llm_usage import aggregate_usage
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.locks import LockCancelled

    cancelled = threading.Event()
    model_service.delay = 0.2
    model_service.replies.append((200, response()))
    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service, cancelled=cancelled.is_set)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(executor.complete, "compile", request())
            for _ in range(100):
                if model_service.requests:
                    break
                cancelled.wait(0.01)
            cancelled.set()
            with pytest.raises(LockCancelled):
                future.result(timeout=5)
    assert executor.calls == 1
    assert aggregate_usage(kb_dir)["input_total"] == 12


@pytest.mark.asyncio
async def test_cancellation_interrupts_async_request_and_does_not_retry(model_service):
    from openkb.locks import LockCancelled

    cancelled = threading.Event()
    model_service.delay = 0.2
    model_service.replies.append((200, response()))
    executor = executor_for(model_service, cancelled=cancelled.is_set)
    task = asyncio.create_task(executor.acomplete("compile", request()))
    for _ in range(100):
        if model_service.requests:
            break
        await asyncio.sleep(0.01)
    cancelled.set()
    with pytest.raises(LockCancelled):
        await task
    assert len(model_service.requests) == executor.calls == 1


def test_task_freezes_role_binding_and_new_task_reads_new_settings(model_service, kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    config = kb_dir / ".openkb/config.yaml"
    config.write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nconversation_model: openai/gpt-4o\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=first\nOPENAI_API_BASE={model_service.url}\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        config.write_text("model: openai/gpt-4o\nlanguage: en\n")
        (kb_dir / ".env").write_text(f"LLM_API_KEY=second\nOPENAI_API_BASE={model_service.url}\n")
        assert first.executor.bindings.model("compile") == "openai/gpt-4o-mini"
        assert first.executor.bindings.model("conversation_index") == "openai/gpt-4o"
        assert first.executor.bindings.credentials().api_key == "first"
    second = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), second.begin(kb_dir):
        assert second.executor.bindings.credentials().api_key == "second"
        assert second.executor.bindings.model("compile") == "openai/gpt-4o"


def test_bound_requests_are_immutable_and_ledger_has_no_secrets(model_service, kb_dir):
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.append((200, response(usage=False)))
    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service)
        executor.bindings.credentials().extra_headers["x-leak"] = "mutated"
        result = executor.complete("document_index", request())
    assert result.usage is None and not result.raw_usage_available
    assert len(read_requests(kb_dir)) == 1
    serialized = read_requests(kb_dir)[0].model_dump_json()
    assert (
        "hello" not in serialized
        and "api_key" not in serialized
        and model_service.url not in serialized
    )
    assert "x-leak" not in str(model_service.records[0][1])


@pytest.mark.asyncio
async def test_deadline_stops_retry_and_releases_concurrency_slot(model_service):
    import time

    from openkb.llm_execution import ModelDeadlineExceeded

    model_service.delay = 0.2
    model_service.replies.extend([(200, response())] * 3)
    executor = executor_for(model_service, deadline=time.monotonic() + 0.06, concurrency=1)
    with pytest.raises(ModelDeadlineExceeded):
        await executor.acomplete("compile", request())
    with pytest.raises(ModelDeadlineExceeded):
        await executor.acomplete("compile", request())
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_async_compiler_uses_executor_and_terminal_errors_do_not_degrade(model_service):
    from openkb.agent.compiler import _llm_call_async
    from openkb.llm_execution import ModelBudgetExceeded

    model_service.replies.append((200, response()))
    executor = executor_for(model_service, max_calls=1)
    with executor.activate():
        assert (
            await _llm_call_async("ignored", [{"role": "user", "content": "hello"}], "concept")
            == "ok"
        )
        with pytest.raises(ModelBudgetExceeded):
            await _llm_call_async("ignored", [{"role": "user", "content": "repair"}], "repair")
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_real_compiler_stops_on_exhausted_budget(model_service, kb_dir):
    import json

    from openkb.agent.compiler import compile_short_doc
    from openkb.llm_execution import ModelBudgetExceeded

    source = kb_dir / "wiki/sources/limited.md"
    source.write_text("# Limited\nA source for the compiler.")
    summary = {"description": "Summary", "content": "# Summary\nBody"}
    plan = {"create": [{"name": "topic", "title": "Topic"}], "update": [], "related": []}
    for content in (summary, plan):
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(content)}))
        )
    executor = executor_for(model_service, max_calls=2)
    with executor.activate(), pytest.raises(ModelBudgetExceeded):
        await compile_short_doc("limited", source, kb_dir, "openai/gpt-4o-mini")
    assert len(model_service.requests) == 2
    assert not (kb_dir / "wiki/concepts/topic.md").exists()


@pytest.mark.parametrize(
    "policy",
    [
        {"max_calls": -1},
        {"concurrency": 0},
        {"deadline_seconds": float("nan")},
        {"api_key": "secret"},
    ],
)
def test_invalid_model_policy_is_rejected_before_task_start(policy):
    from openkb.config import validate_runtime_config

    with pytest.raises(ValueError) as caught:
        validate_runtime_config({"model": "gpt-4o", "language": "en", "model_policy": policy})
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_redirect_cannot_send_an_unbudgeted_request(model_service, asynchronous):
    model_service.replies.extend([(307, {}), (200, response())])
    executor = executor_for(model_service, max_calls=1)
    with pytest.raises(Exception):
        if asynchronous:
            await executor.acomplete("compile", request())
        else:
            executor.complete("compile", request())
    assert len(model_service.requests) == executor.calls == 1


def test_anthropic_cache_usage_is_normalized_from_transport(model_service, kb_dir):
    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, RoleBindings
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.append(
        (
            200,
            {
                "id": "anthropic-fixture",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 3,
                },
            },
        )
    )
    with import_usage_execution(kb_dir, "fixture"):
        executor = CompletionExecutor(
            RoleBindings(
                "anthropic/claude-sonnet-4-5",
                LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
            )
        )
        executor.complete("compile", request())
    (record,) = read_requests(kb_dir)
    assert record.input_total == 60
    assert record.cached_input == 30
    assert not record.application_cache_hit


def test_model_table_identity_is_frozen_from_snapshot(model_service, kb_dir, monkeypatch):
    from litellm.litellm_core_utils import get_model_cost_map

    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        original = context.snapshot.values()["model_table"]
        monkeypatch.setattr(
            get_model_cost_map, "get_model_cost_map_source_info", lambda: {"changed": True}
        )
        assert context.executor.bindings.public_identity()["model_table"] == original
