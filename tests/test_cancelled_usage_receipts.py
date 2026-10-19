"""Real provider receipts survive cancellation, duplicate delivery and serialization."""

import asyncio
import threading
import time

import pytest
from test_llm_execution import executor_for, request

pytest_plugins = ("test_vendor_sdk",)


@pytest.fixture
def provider_receipt(model_service):
    """Warm the exact request with an actual completion, keeping its usage receipt."""
    result = executor_for(model_service).complete("compile", request())
    assert result.raw_usage_available and result.usage.prompt_tokens > 0
    model_service.requests.clear()
    model_service.intervals.clear()
    model_service.records.clear()
    return result.response


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,known", [(0.08, True), (0.8, False)])
async def test_async_cancel_preserves_late_receipt_without_publishing(
    model_service, kb_dir, provider_receipt, delay, known
):
    from native_usage_fixtures import aggregate_usage, native_usage_execution, read_requests

    from ultrakb.locks import LockCancelled

    stop = threading.Event()
    model_service.delay = delay
    with native_usage_execution(kb_dir, "cancelled"):
        executor = executor_for(model_service, retries=2, cancelled=stop.is_set)
        task = asyncio.create_task(executor.acomplete("compile", request()))
        while not model_service.requests:
            await asyncio.sleep(0.005)
        began = time.monotonic()
        stop.set()
        with pytest.raises(LockCancelled):
            await task
        assert time.monotonic() - began < 0.7
    [record] = read_requests(kb_dir)
    assert executor.calls == 1
    assert record.business_state == "cancelled" and record.send_status == "sent"
    assert aggregate_usage(kb_dir)["usage_complete"] is known
    assert record.input_total == (provider_receipt.usage.prompt_tokens if known else None)
    assert len(model_service.requests) == 1


def test_cancelled_without_delivered_receipt_remains_unknown(kb_dir):
    from native_usage_fixtures import aggregate_usage, native_usage_execution, read_requests

    from ultrakb.llm_usage import begin_request, finish_request

    with native_usage_execution(kb_dir, "cancelled-unconfirmed") as scope:
        identity = begin_request("deepseek/deepseek-flash", "answer", scope=scope)
        finish_request(identity, scope=scope, state="cancelled", business_state="cancelled")
    [record] = read_requests(kb_dir)
    assert record.business_state == "cancelled"
    assert record.input_total is None and record.output_total is None
    assert not aggregate_usage(kb_dir)["usage_complete"]


@pytest.mark.asyncio
async def test_repeated_cancel_and_duplicate_receipts_do_not_lose_or_duplicate_usage(
    model_service, kb_dir, provider_receipt
):
    from native_usage_fixtures import aggregate_usage, native_usage_execution, read_requests

    from ultrakb.llm_usage import finish_request

    model_service.delay = 0.08
    with native_usage_execution(kb_dir, "cancelled") as scope:
        executor = executor_for(model_service)
        task = asyncio.create_task(executor.acomplete("compile", request()))
        while not model_service.requests:
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        [record] = read_requests(kb_dir)
        finish_request(record.request_attempt_id, provider_receipt, scope=scope)
        # An absent late receipt is a ledger input; it must never erase real totals.
        finish_request(record.request_attempt_id, scope=scope)
    [record] = read_requests(kb_dir)
    assert record.input_total == provider_receipt.usage.prompt_tokens
    assert record.output_total == provider_receipt.usage.completion_tokens
    assert record.business_state == "cancelled"
    assert aggregate_usage(kb_dir)["actual_sends"] == 1
    assert aggregate_usage(kb_dir)["input_total"] == provider_receipt.usage.prompt_tokens


@pytest.mark.asyncio
async def test_cancelled_stream_headers_drain_only_usage(model_service, kb_dir, live_llm):
    import httpx
    from native_usage_fixtures import native_usage_execution, read_requests

    from ultrakb.llm_usage_transport import observe_model_call

    payload = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Reply exactly ok in lowercase."}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 64,
        "thinking": {"type": "disabled"},
    }
    warmed = await live_llm.remote_acompletion(**payload)
    async for _ in warmed:
        pass
    await warmed.aclose()
    model_service.requests.clear()
    model_service.delay = 0.06
    with native_usage_execution(kb_dir, "stream"):
        async with httpx.AsyncClient() as client:
            with observe_model_call("deepseek/deepseek-flash", "answer"):
                pending = asyncio.create_task(
                    client.send(
                        client.build_request(
                            "POST",
                            model_service.url + "/chat/completions",
                            json=payload,
                            headers={
                                "Authorization": "Bearer " + model_service.credentials.api_key
                            },
                        ),
                        stream=True,
                    )
                )
                while not model_service.requests:
                    await asyncio.sleep(0.005)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
    [record] = read_requests(kb_dir)
    assert record.usage_status == "known", record
    assert record.business_state == "cancelled"
    assert record.input_total > 0 and record.output_total > 0


def test_budget_and_phase_receipt_survive_runtime_serialization(kb_dir, model_service):
    import json

    from native_catalog_fixtures import catalog

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.ingest_result import ImportUnitOutcome
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.runtime.records import UnitResult
    from ultrakb.storage.usage_store import UsageStore

    context = ExecutionContext(executor=executor_for(model_service, max_calls=5))
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        context.executor.complete("compile", request())
        receipt = UsageStore(kb_dir, catalog(kb_dir).identity).receipt(
            execution_id=context.storage_execution.execution_id
        )
    result = UnitResult(
        "failed",
        model_usage=receipt,
        import_units=(
            ImportUnitOutcome(
                "unit",
                "failed",
                "revision",
                completed_stages=("conversion", "indexing"),
                recovery_point=".ultrakb/index-checkpoints/source/revision/checkpoint.json",
            ),
        ),
    )
    restored = UnitResult.from_summary(json.loads(json.dumps(result.summary())))
    assert restored == result
    assert restored.model_usage["current"]["actual_sends"] == 1
    assert restored.model_usage["model_tasks"][0]["actual_sends"] == 1
    assert restored.model_usage["model_tasks"][0]["limit"] == 5
