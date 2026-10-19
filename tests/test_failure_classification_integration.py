"""Actual SDK failures retain their category through retries and the usage ledger."""

import asyncio

import litellm
import pytest
from error_transport import ErrorTransport
from native_usage_fixtures import aggregate_usage, observe_executor, read_requests

from ultrakb.application.connection_failures import connection_failure, shared_model_failure
from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("provider_code", "message", "expected_code", "halt"),
    [
        (
            "rate_limit_exceeded",
            "Token quota per minute exceeded. Please retry later.",
            "rate_limit",
            False,
        ),
        ("rate_limit_exceeded", "每分钟额度耗尽，请稍后重试。", "rate_limit", False),
        (
            "rate_limit_exceeded",
            "Token quota for the billing project per minute exceeded. Retry later.",
            "rate_limit",
            False,
        ),
        ("insufficient_quota", "The request could not be processed.", "quota", True),
    ],
)
def test_real_rate_limit_errors_reach_batch_decision_without_losing_the_send_count(
    kb_dir, record_property, asynchronous, provider_code, message, expected_code, halt
):
    published = []
    body = {"error": {"message": message, "type": provider_code, "code": provider_code}}
    with ErrorTransport(status=429, body=body, headers={"Retry-After": "1"}) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                LlmCredentialBundle(
                    api_key="offline-classification-key", base_url=service.url, timeout=1
                ),
            ),
            ModelCallPolicy(max_calls=3, retries=1, backoff=0),
        )
        observe_executor(kb_dir, executor)
        request = ModelRequest(
            [{"role": "user", "content": "This local endpoint only returns errors."}],
            operation="query",
            stage="answer",
            generation_options={"max_tokens": 16},
        )
        with pytest.raises(litellm.RateLimitError) as caught:
            if asynchronous:
                asyncio.run(executor.acomplete("answer", request, on_response=published.append))
            else:
                executor.complete("answer", request, on_response=published.append)
        assert caught.value.status_code == 429
        assert len(service.requests) == executor.calls == 2
        assert len(executor.model_errors) == 2
        assert all(error.status_code == 429 for error in executor.model_errors)
        record_property("sdk_outer_code", str(getattr(caught.value, "code", None)))
        record_property("sdk_context_class", type(caught.value.__context__).__name__)
        assert all(
            connection_failure(error).code == expected_code for error in executor.model_errors
        )
        failure = shared_model_failure(executor.model_errors)
        assert (failure is not None) is halt
        if failure is not None:
            assert failure.stops_batch and failure.code == expected_code
            assert message not in failure.message
        record_property("http_sends", len(service.requests))
        record_property("application_category", expected_code)
        record_property("batch_halted", halt)
    assert published == []
    records = read_requests(kb_dir)
    assert len(records) == 2 and len({item.logical_call_id for item in records}) == 1
    assert all(item.send_status == "sent" and item.state == "failed" for item in records)
    assert all(item.usage_status == "unknown" and item.input_total is None for item in records)
    summary = aggregate_usage(kb_dir)
    assert summary["actual_sends"] == summary["unknown_requests"] == 2
    assert summary["in_flight_requests"] == 0 and not summary["usage_complete"]
