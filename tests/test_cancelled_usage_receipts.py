"""Real transport receipts survive bounded cancellation cleanup."""

import asyncio
import threading
import time

import pytest
from test_llm_execution import executor_for, request
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delay,usage,known", [(0.08, True, True), (0.08, False, False), (0.8, True, False)]
)
async def test_async_cancel_preserves_late_receipt_without_publishing(
    model_service, kb_dir, delay, usage, known
):
    from openkb.llm_usage import aggregate_usage, read_requests
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.locks import LockCancelled

    stop = threading.Event()
    model_service.delay = delay
    model_service.replies.append((200, response(usage=usage)))
    with import_usage_execution(kb_dir, "cancelled"):
        executor = executor_for(model_service, retries=2, cancelled=stop.is_set)
        task = asyncio.create_task(executor.acomplete("compile", request()))
        while not model_service.requests:
            await asyncio.sleep(0.005)
        began = time.monotonic()
        stop.set()
        with pytest.raises(LockCancelled):
            await task
        assert time.monotonic() - began < 0.6
    records = read_requests(kb_dir)
    assert len(records) == executor.calls == 1
    assert records[0].business_state == "cancelled"
    assert records[0].send_status == "sent"
    assert aggregate_usage(kb_dir)["usage_complete"] is known
    assert records[0].input_total == (12 if known else None)
    assert len(model_service.requests) == 1


@pytest.mark.asyncio
async def test_repeated_cancel_and_duplicate_receipts_do_not_lose_or_duplicate_usage(
    model_service, kb_dir
):
    from openkb.llm_usage import aggregate_usage, finish_request, read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.delay = 0.08
    model_service.replies.append((200, response()))
    with import_usage_execution(kb_dir, "cancelled") as scope:
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
        finish_request(record.request_attempt_id, response(), scope=scope)
        finish_request(
            record.request_attempt_id,
            {"id": record.provider_request_id, "usage": None},
            scope=scope,
        )
    [record] = read_requests(kb_dir)
    assert record.input_total == 12 and record.output_total == 3
    assert record.business_state == "cancelled"
    assert aggregate_usage(kb_dir)["actual_sends"] == 1
    assert aggregate_usage(kb_dir)["input_total"] == 12


@pytest.mark.asyncio
async def test_cancelled_stream_headers_drain_only_usage(model_service, kb_dir):
    import httpx
    from test_vendor_sdk import streamed_response

    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution
    from openkb.llm_usage_transport import observe_model_call

    model_service.delay = 0.06
    model_service.replies.append(
        (200, streamed_response({"role": "assistant", "content": "discard"}))
    )
    with import_usage_execution(kb_dir, "stream"):
        async with httpx.AsyncClient() as client:
            with observe_model_call("fixture", "answer"):
                pending = asyncio.create_task(
                    client.send(
                        client.build_request(
                            "POST",
                            model_service.url + "/chat/completions",
                            json={"model": "fixture", "messages": [], "stream": True},
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
    assert record.usage_status == "known"
    assert record.business_state == "cancelled"
    assert record.input_total == 12


def test_budget_and_phase_receipt_survive_runtime_serialization(kb_dir, model_service):
    import json

    from openkb.application.execution import ExecutionContext
    from openkb.ingest_result import ImportUnitOutcome
    from openkb.llm_usage import usage_receipt
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.records import UnitResult

    context = ExecutionContext(executor=executor_for(model_service, max_calls=5))
    model_service.replies.append((200, response()))
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        context.executor.complete("compile", request())
        receipt = usage_receipt(kb_dir)
    result = UnitResult(
        "failed",
        model_usage=receipt,
        import_units=(
            ImportUnitOutcome(
                "unit",
                "failed",
                "revision",
                completed_stages=("conversion", "indexing"),
                recovery_point=".openkb/index-checkpoints/source/revision/checkpoint.json",
            ),
        ),
    )
    restored = UnitResult.from_summary(json.loads(json.dumps(result.summary())))
    assert restored == result
    assert restored.model_usage["current"]["actual_sends"] == 1
    assert restored.model_usage["model_tasks"][0]["actual_sends"] == 1
    assert restored.model_usage["model_tasks"][0]["limit"] == 5
