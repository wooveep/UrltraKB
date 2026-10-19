"""Local integration: genuine SDK calls to an error-only HTTP endpoint."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import litellm
import pytest
from error_transport import ErrorTransport
from native_usage_fixtures import aggregate_usage, observe_executor, read_requests

from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import (
    CompletionExecutor,
    ModelBudgetExceeded,
    ModelCallPolicy,
    ModelRequest,
    RoleBindings,
)
from ultrakb.locks import LockCancelled

pytestmark = pytest.mark.integration


def executor_for(service, kb_dir, *, timeout=1, **policy):
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(api_key="offline-test-key", base_url=service.url, timeout=timeout),
        ),
        ModelCallPolicy(**{"backoff": 0, **policy}),
    )

    return observe_executor(kb_dir, executor)


def request():
    return ModelRequest(
        [{"role": "user", "content": "This local endpoint must only return an error."}],
        operation="query",
        stage="answer",
        generation_options={"max_tokens": 16},
    )


def invoke(executor, *, asynchronous=False, on_response=None):
    if asynchronous:
        return asyncio.run(executor.acomplete("answer", request(), on_response=on_response))
    return executor.complete("answer", request(), on_response=on_response)


def retry_after_values(error):
    """Read exposed SDK/cause metadata without inventing missing provider headers."""
    seen = set()
    pending = [error]
    values = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        for headers in (
            getattr(current, "headers", None),
            getattr(current, "litellm_response_headers", None),
            getattr(response, "headers", None),
        ):
            if headers and (value := headers.get("retry-after")) is not None:
                values.append(value)
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
    return values


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("status", "error_type"), [(400, litellm.BadRequestError), (401, litellm.AuthenticationError)]
)
def test_nonretryable_http_errors_keep_sdk_status_and_message_without_success(
    kb_dir, record_property, status, error_type, asynchronous
):
    published = []
    with ErrorTransport(status=status) as service:
        executor = executor_for(service, kb_dir, retries=2)
        with pytest.raises(error_type) as caught:
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert caught.value.status_code == status
        assert service.body["error"]["message"] in str(caught.value)
        assert service.requests[0]["path"] == "/v1/chat/completions"
        headers = {name.lower(): value for name, value in service.requests[0]["headers"].items()}
        assert headers["authorization"] == "Bearer offline-test-key"
        assert len(service.requests) == executor.calls == 1
        record_property("http_sends", len(service.requests))
        record_property("sdk_error_code", str(getattr(caught.value, "code", None)))
        record_property("sdk_error_body", str(getattr(caught.value, "body", None)))
    assert not published
    [record] = read_requests(kb_dir)
    assert record.send_status == "sent" and record.state == "failed"
    assert record.usage_status == "unknown"
    assert record.input_total is None and record.output_total is None


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (429, litellm.RateLimitError),
        (500, litellm.InternalServerError),
        (503, litellm.ServiceUnavailableError),
    ],
)
@pytest.mark.parametrize(("retries", "expected_sends"), [(0, 1), (1, 2), (2, 3)])
def test_retryable_http_errors_obey_application_retry_limit_and_keep_usage_unknown(
    kb_dir, record_property, status, error_type, retries, expected_sends, asynchronous
):
    published = []
    with ErrorTransport(status=status, headers={"Retry-After": "7"}) as service:
        executor = executor_for(service, kb_dir, retries=retries)
        with pytest.raises(error_type) as caught:
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert caught.value.status_code == status
        assert service.body["error"]["message"] in str(caught.value)
        retry_after = retry_after_values(caught.value)
        assert all(value == "7" for value in retry_after)
        if status == 429:
            assert retry_after, "The pinned SDK must retain the rate-limit retry hint"
        record_property("sdk_retry_after", json.dumps(retry_after))
        record_property("http_sends", len(service.requests))
        assert len(service.requests) == executor.calls == expected_sends
    assert not published
    records = read_requests(kb_dir)
    assert len(records) == expected_sends
    assert len({record.logical_call_id for record in records}) == 1
    assert all(record.send_status == "sent" and record.state == "failed" for record in records)
    assert all(record.usage_status == "unknown" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(("retries", "expected_sends"), [(0, 1), (1, 2)])
def test_real_http_timeout_retries_only_within_the_task_policy(
    kb_dir, record_property, retries, expected_sends, asynchronous
):
    published = []
    with ErrorTransport(status=504, gate=threading.Event()) as service:
        executor = executor_for(service, kb_dir, timeout=0.05, retries=retries)
        with pytest.raises(litellm.Timeout):
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert service.wait_for_requests(expected_sends)
        assert len(service.requests) == executor.calls == expected_sends
        record_property("http_sends", len(service.requests))
    assert not published
    records = read_requests(kb_dir)
    assert len(records) == expected_sends
    assert all(record.state == "failed" and record.send_status == "sent" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    assert aggregate_usage(kb_dir)["actual_sends"] == expected_sends
    assert not aggregate_usage(kb_dir)["usage_complete"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stop_source", ["executor", "policy"])
def test_stop_after_a_real_http_send_prevents_error_retries(
    kb_dir, record_property, stop_source, asynchronous
):
    stopped = threading.Event()
    published = []
    with ErrorTransport(status=503, gate=threading.Event()) as service:
        executor = executor_for(service, kb_dir, retries=3, cancelled=stopped.is_set)
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(
                invoke, executor, asynchronous=asynchronous, on_response=published.append
            )
            try:
                assert service.wait_for_requests(1)
                assert not pending.done()
                if stop_source == "executor":
                    executor.cancel()
                else:
                    stopped.set()
                service.gate.set()
                with pytest.raises(LockCancelled):
                    pending.result(timeout=3)
            finally:
                service.gate.set()
        with pytest.raises(LockCancelled):
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert len(service.requests) == executor.calls == 1
        record_property("http_sends", len(service.requests))
    assert not published
    [record] = read_requests(kb_dir)
    assert record.state in {"failed", "cancelled"}
    assert record.business_state in {"failed", "cancelled"}
    assert record.send_status == "sent" and record.usage_status == "unknown"
    assert record.input_total is None and record.output_total is None


def test_cancelling_a_task_waiting_for_real_http_headers_does_not_retry(kb_dir, record_property):
    published = []
    with ErrorTransport(status=503, gate=threading.Event()) as service:
        executor = executor_for(service, kb_dir, retries=3)

        async def cancel_waiting_task():
            pending = asyncio.create_task(
                executor.acomplete("answer", request(), on_response=published.append)
            )
            try:
                assert await asyncio.to_thread(service.wait_for_requests, 1)
                assert not pending.done()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 3)
            finally:
                service.gate.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        asyncio.run(cancel_waiting_task())
        assert len(service.requests) == executor.calls == 1
        record_property("http_sends", len(service.requests))
    assert not published
    [record] = read_requests(kb_dir)
    assert record.state == "cancelled" and record.send_status == "sent"
    assert record.business_state == "cancelled" and record.usage_status == "unknown"
    assert record.input_total is None and record.output_total is None
    assert not aggregate_usage(kb_dir)["usage_complete"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(("max_calls", "expected_sends"), [(0, 0), (1, 1), (2, 2)])
def test_task_send_allowance_stops_real_error_retries_and_cannot_be_reopened(
    kb_dir, record_property, max_calls, expected_sends, status, asynchronous
):
    published = []
    with ErrorTransport(status=status) as service:
        executor = executor_for(service, kb_dir, retries=3, max_calls=max_calls)
        with pytest.raises(ModelBudgetExceeded):
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert len(service.requests) == executor.calls == expected_sends
        with pytest.raises(ModelBudgetExceeded):
            invoke(executor, asynchronous=asynchronous, on_response=published.append)
        assert len(service.requests) == executor.calls == expected_sends
        record_property("http_sends", len(service.requests))
    assert not published
    records = read_requests(kb_dir)
    assert len(records) == expected_sends
    assert all(record.state == "failed" and record.send_status == "sent" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    assert aggregate_usage(kb_dir)["actual_sends"] == expected_sends
