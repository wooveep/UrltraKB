"""Error rules consume SDK error data without credentials or network access."""

import json

import httpx
import litellm
import openai
import pytest

from ultrakb.application.connection_failures import connection_failure, shared_model_failure

pytestmark = pytest.mark.unit


def rate_error(message, *, code="rate_limit_exceeded"):
    response = httpx.Response(
        429, request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions")
    )
    return openai.RateLimitError(message, response=response, body={"code": code})


@pytest.mark.parametrize(
    "message",
    [
        "Token quota per minute exceeded. Please retry after 1 second.",
        "Request quota per hour exceeded; retry later.",
        "每分钟请求额度已用尽，请稍后重试。",
    ],
)
def test_transient_rate_limits_cannot_become_permanent_batch_failures(message):
    error = rate_error(message)
    failure = connection_failure(error)
    assert failure.code == "rate_limit"
    assert not failure.stops_batch
    assert shared_model_failure([error]) is None
    assert message not in failure.message


@pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached"])
def test_billing_failure_uses_structured_sdk_code_even_when_message_is_generic(code):
    error = rate_error("The request could not be processed.", code=code)
    failure = connection_failure(error)
    assert failure.code == "quota" and failure.stops_batch
    assert shared_model_failure([error]) == failure


@pytest.mark.parametrize(
    "message", ["insufficient_quota", "Insufficient balance", "账户余额不足，请充值。"]
)
def test_billing_fallback_without_structured_code_still_stops_the_batch(message):
    failure = connection_failure(rate_error(message, code=None))
    assert failure.code == "quota" and failure.stops_batch


def test_payment_required_status_stops_the_batch_without_message_keywords():
    response = httpx.Response(402, request=httpx.Request("POST", "http://127.0.0.1/v1"))
    error = openai.APIStatusError("The request was rejected.", response=response, body={})
    failure = connection_failure(error)
    assert failure.code == "quota" and failure.stops_batch
    assert shared_model_failure([error]) == failure


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("rate_limit_exceeded", "每分钟额度耗尽，请稍后重试。", "rate_limit"),
        ("insufficient_quota", "The request was rejected.", "quota"),
    ],
)
def test_wrapped_sdk_error_uses_inner_structured_code_and_tolerates_a_cycle(
    link, code, message, expected
):
    original = rate_error(message, code=code)
    wrapped = litellm.RateLimitError(message, llm_provider="deepseek", model="deepseek-flash")
    setattr(wrapped, link, original)
    original.__context__ = wrapped
    assert wrapped.code == "429" and wrapped.body is None
    failure = connection_failure(wrapped)
    assert failure.code == expected
    assert failure.stops_batch is (expected == "quota")


def test_litellm_retained_json_payload_keeps_its_code_when_sdk_body_is_empty():
    from litellm.llms.openai.common_utils import OpenAIError

    payload = {
        "error": {
            "code": "rate_limit_exceeded",
            "message": "Token quota for the billing project per minute exceeded. Retry later.",
        }
    }
    error = OpenAIError(status_code=429, message=json.dumps(payload))
    assert error.body is None
    failure = connection_failure(error)
    assert failure.code == "rate_limit" and not failure.stops_batch
    assert shared_model_failure([error]) is None
