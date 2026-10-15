"""Task policies are enforced at the actual SDK transport boundary."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest_plugins = ("test_vendor_sdk",)


def executor_for(service, **policy):
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    return CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", service.credentials), ModelCallPolicy(**policy)
    )


@pytest.mark.parametrize("effort", ["low", "none"])
@pytest.mark.asyncio
async def test_captured_role_options_stay_frozen_during_a_real_small_request(
    kb_dir, model_service, effort
):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_execution import ModelRequest
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "role_generation:\n  knowledge_retrieval:\n"
        f"    reasoning_effort: {effort}\n    max_tokens: 4096\n"
        "  answer:\n    max_tokens: 7000\n",
    )
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        frozen = context.executor.fingerprint
        query = ModelRequest(
            [{"role": "user", "content": "Reply exactly ok."}], operation="query", stage="answer"
        )
        retrieval = context.executor._kwargs("knowledge_retrieval", query)
        compiled = context.executor._kwargs("compile", query)
        answer = context.executor._kwargs("answer", query)
        assert retrieval["extra_body"]["reasoning_effort"] == effort
        assert retrieval["max_tokens"] == 4096
        assert "extra_body" not in compiled and "extra_body" not in answer
        assert answer["max_tokens"] == 7000
        atomic_write_text(
            kb_dir / ".ultrakb/config.yaml", "model: deepseek/deepseek-flash\nlanguage: en\n"
        )
        result = await context.executor.acomplete("answer", request())
        assert result.text.strip().lower() == "ok"
        assert context.executor.policy.retrieval_tokens == 1024
    assert model_service.requests[-1]["model"] == "deepseek-flash"
    assert model_service.requests[-1]["max_tokens"] == 64
    other = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), other.begin(kb_dir):
        assert other.executor.fingerprint != frozen


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": {}},
        {"knowledge_retrieval": {"drop_params": True}},
        {"knowledge_retrieval": {"reasoning_effort": "invalid"}},
        {"knowledge_retrieval": {"max_tokens": 8192}},
    ],
)
def test_invalid_role_options_fail_before_execution(options):
    from ultrakb.config_validation import validate_runtime_config

    with pytest.raises(ValueError, match="role_generation"):
        validate_runtime_config(
            {"model": "openai/gpt-4o-mini", "language": "en", "role_generation": options}
        )


def request(**values):
    from ultrakb.llm_execution import ModelRequest

    options = values.pop("generation_options", {"max_tokens": 64})
    return ModelRequest(
        [{"role": "user", "content": "Reply exactly ok in lowercase."}],
        operation="compile",
        stage="summary",
        generation_options=options,
        **values,
    )


def test_retry_attempts_share_logical_call_and_do_not_double_bill(
    model_service, kb_dir, monkeypatch
):
    import httpx

    from ultrakb.llm_usage import aggregate_usage, read_requests
    from ultrakb.llm_usage_execution import import_usage_execution

    send = httpx.Client._send_single_request
    failures = []

    def disconnect_once(client, outgoing):
        if outgoing.url.host == "127.0.0.1" and not failures:
            failures.append(True)
            raise httpx.ConnectError(
                "Local transport unavailable before connection", request=outgoing
            )
        return send(client, outgoing)

    monkeypatch.setattr(httpx.Client, "_send_single_request", disconnect_once)
    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service, max_calls=2, retries=1, backoff=0)
        result = executor.complete("compile", request(parent_call_id="a" * 32))
    records = read_requests(kb_dir)
    assert result.text.strip().lower() == "ok" and result.raw_usage_available
    assert executor.calls == len(records) == 2 and len(model_service.requests) == 1
    assert len({record.logical_call_id for record in records}) == 1
    assert all(
        record.parent_call_id == "a" * 32 and record.operation == "compile" for record in records
    )
    assert aggregate_usage(kb_dir)["input_total"] == result.usage.prompt_tokens


@pytest.mark.asyncio
async def test_parallel_calls_cannot_overspend_budget(model_service):
    from ultrakb.llm_execution import ModelBudgetExceeded

    executor = executor_for(model_service, max_calls=2, concurrency=4)
    results = await asyncio.gather(
        *(executor.acomplete("compile", request()) for _ in range(4)), return_exceptions=True
    )
    assert len(model_service.requests) == 2
    assert sum(isinstance(result, ModelBudgetExceeded) for result in results) == 2
    assert all(
        result.text.strip().lower() == "ok"
        for result in results
        if not isinstance(result, Exception)
    )


def test_algorithm_cannot_override_transport_binding():
    with pytest.raises(ValueError, match="generation"):
        request(generation_options={"api_key": "injected"})


def test_compiler_uses_the_active_budget_for_repair_calls(model_service):
    from ultrakb.agent.compiler import _llm_call
    from ultrakb.llm_execution import ModelBudgetExceeded

    executor = executor_for(model_service, max_calls=1)
    with executor.activate():
        assert (
            _llm_call(
                "ignored-by-frozen-binding",
                request().payload()["messages"],
                "summary",
                max_tokens=64,
            )
            .strip()
            .lower()
            == "ok"
        )
        with pytest.raises(ModelBudgetExceeded):
            _llm_call("ignored", [{"role": "user", "content": "repair"}], "repair")
    assert len(model_service.requests) == 1


@pytest.mark.parametrize("status", [401, 400, 500, 503])
@pytest.mark.unit
def test_retry_policy_classifies_transient_transport_errors(status):
    from ultrakb.llm_execution import ModelCallPolicy
    from ultrakb.llm_usage_transport import _failure_state
    from ultrakb.locks import LockCancelled

    class TransportFailure(Exception):
        status_code = status

    # Retry classification consumes transport errors, never invented model answers.
    policy = ModelCallPolicy(max_calls=4, retries=1, backoff=0)
    error = TransportFailure()
    assert policy.retryable(error) == (status >= 500)
    assert _failure_state(error) == "failed"
    assert _failure_state(LockCancelled("stopped")) == "cancelled"


def test_late_sync_result_is_recorded_but_never_published(model_service, kb_dir):
    from ultrakb.llm_usage import aggregate_usage
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.locks import LockCancelled

    # Warm this exact genuine request so the timed test controls only local delivery.
    warm = executor_for(model_service).complete("compile", request())
    model_service.requests.clear()
    model_service.delay = 0.2
    cancelled = threading.Event()
    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service, cancelled=cancelled.is_set)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(executor.complete, "compile", request())
            for _ in range(200):
                if model_service.requests:
                    break
                cancelled.wait(0.01)
            assert model_service.requests
            cancelled.set()
            with pytest.raises(LockCancelled):
                future.result(timeout=5)
    assert executor.calls == 1
    assert aggregate_usage(kb_dir)["input_total"] == warm.usage.prompt_tokens


@pytest.mark.asyncio
async def test_cancellation_interrupts_async_request_and_does_not_retry(model_service):
    from ultrakb.locks import LockCancelled

    await executor_for(model_service).acomplete("compile", request())
    model_service.requests.clear()
    model_service.delay = 0.2
    cancelled = threading.Event()
    executor = executor_for(model_service, cancelled=cancelled.is_set)
    task = asyncio.create_task(executor.acomplete("compile", request()))
    for _ in range(200):
        if model_service.requests:
            break
        await asyncio.sleep(0.01)
    assert model_service.requests
    cancelled.set()
    with pytest.raises(LockCancelled):
        await task
    assert len(model_service.requests) == executor.calls == 1


def test_task_freezes_role_binding_and_new_task_reads_new_settings(model_service, kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    config = kb_dir / ".ultrakb/config.yaml"
    config.write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nconversation_model: openai/gpt-4o\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=first\nOPENAI_API_BASE={model_service.url}\n")
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), first.begin(kb_dir):
        config.write_text("model: openai/gpt-4o\nlanguage: en\n")
        (kb_dir / ".env").write_text(f"LLM_API_KEY=second\nOPENAI_API_BASE={model_service.url}\n")
        assert first.executor.bindings.model("compile") == "openai/gpt-4o-mini"
        assert first.executor.bindings.model("conversation_index") == "openai/gpt-4o"
        assert first.executor.bindings.credentials().api_key == "first"
    second = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), second.begin(kb_dir):
        assert second.executor.bindings.credentials().api_key == "second"
        assert second.executor.bindings.model("compile") == "openai/gpt-4o"


def test_bound_requests_are_immutable_and_ledger_has_no_secrets(model_service, kb_dir):
    from ultrakb.llm_usage import normalize_usage, read_requests
    from ultrakb.llm_usage_execution import import_usage_execution

    with import_usage_execution(kb_dir, "fixture"):
        executor = executor_for(model_service)
        executor.bindings.credentials().extra_headers["x-leak"] = "mutated"
        result = executor.complete("document_index", request())
    assert result.raw_usage_available and result.usage.prompt_tokens > 0
    assert normalize_usage(None)["usage_status"] == "unknown"
    assert len(read_requests(kb_dir)) == 1
    serialized = read_requests(kb_dir)[0].model_dump_json()
    assert "api_key" not in serialized and model_service.url not in serialized
    assert model_service.credentials.api_key not in serialized
    assert "x-leak" not in str(model_service.records[0][1])


@pytest.mark.asyncio
async def test_deadline_stops_retry_and_releases_concurrency_slot(model_service):
    import time

    from ultrakb.llm_execution import ModelDeadlineExceeded

    await executor_for(model_service).acomplete("compile", request())
    model_service.requests.clear()
    model_service.delay = 0.2
    executor = executor_for(model_service, deadline=time.monotonic() + 0.06, concurrency=1)
    with pytest.raises(ModelDeadlineExceeded):
        await executor.acomplete("compile", request())
    with pytest.raises(ModelDeadlineExceeded):
        await executor.acomplete("compile", request())
    assert len(model_service.requests) == 1
    assert executor._slots.acquire(blocking=False)
    executor._slots.release()


@pytest.mark.asyncio
async def test_async_compiler_uses_executor_and_terminal_errors_do_not_degrade(model_service):
    from ultrakb.agent.compiler import _llm_call_async
    from ultrakb.llm_execution import ModelBudgetExceeded

    executor = executor_for(model_service, max_calls=1)
    with executor.activate():
        assert (
            await _llm_call_async(
                "ignored", request().payload()["messages"], "concept", max_tokens=64
            )
        ).strip().lower() == "ok"
        with pytest.raises(ModelBudgetExceeded):
            await _llm_call_async("ignored", [{"role": "user", "content": "repair"}], "repair")
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_real_compiler_stops_on_exhausted_budget(model_service, kb_dir):
    from ultrakb.agent.compiler import compile_short_doc
    from ultrakb.llm_execution import ModelBudgetExceeded
    from ultrakb.locks import atomic_write_text

    source = kb_dir / "wiki/sources/limited.md"
    atomic_write_text(source, "# Limited\nOrion requires the safety interlock before opening.")
    executor = executor_for(model_service, max_calls=1, retries=0)
    with executor.activate(), pytest.raises(ModelBudgetExceeded):
        await compile_short_doc("limited", source, kb_dir, "deepseek/deepseek-flash")
    assert executor.calls <= 1 and len(model_service.requests) <= 1
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


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
    from ultrakb.config import validate_runtime_config

    with pytest.raises(ValueError) as caught:
        validate_runtime_config({"model": "gpt-4o", "language": "en", "model_policy": policy})
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_redirect_cannot_send_an_unbudgeted_request(model_service, asynchronous, monkeypatch):
    import httpx

    from ultrakb.llm_execution import ModelBudgetExceeded

    flags = []
    if asynchronous:
        original = httpx.AsyncClient._send_handling_redirects

        async def observe(client, outgoing, follow_redirects, history):
            if outgoing.url.host == "127.0.0.1":
                flags.append(follow_redirects)
            return await original(client, outgoing, follow_redirects, history)

        monkeypatch.setattr(httpx.AsyncClient, "_send_handling_redirects", observe)
    else:
        original = httpx.Client._send_handling_redirects

        def observe(client, outgoing, follow_redirects, history):
            if outgoing.url.host == "127.0.0.1":
                flags.append(follow_redirects)
            return original(client, outgoing, follow_redirects, history)

        monkeypatch.setattr(httpx.Client, "_send_handling_redirects", observe)
    executor = executor_for(model_service, max_calls=1)
    if asynchronous:
        await executor.acomplete("compile", request())
        with pytest.raises(ModelBudgetExceeded):
            await executor.acomplete("compile", request())
    else:
        executor.complete("compile", request())
        with pytest.raises(ModelBudgetExceeded):
            executor.complete("compile", request())
    assert flags and flags == [False]
    assert len(model_service.requests) == executor.calls == 1


def test_anthropic_cache_usage_is_normalized_from_transport():
    from ultrakb.llm_usage import normalize_usage

    counters = normalize_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 3,
        }
    )
    assert counters["input_total"] == 60 and counters["cached_input"] == 30
    assert counters["output_total"] == 3 and counters["usage_status"] == "known"


def test_model_table_identity_is_frozen_from_snapshot(model_service, kb_dir, monkeypatch):
    from litellm.litellm_core_utils import get_model_cost_map

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        original = context.snapshot.values()["model_table"]
        monkeypatch.setattr(
            get_model_cost_map, "get_model_cost_map_source_info", lambda: {"changed": True}
        )
        assert context.executor.bindings.public_identity()["model_table"] == original
