"""Exercise admission validation directly, without any provider or fake response."""

import hashlib
import json

import httpx
import pytest
from live_network import _check, admitted_transport
from llm_test_budget import BudgetExceeded, TestRunBudget


def reservation(tmp_path, payload):
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    budget = TestRunBudget(tmp_path / "cost.json")
    identifier = budget.reserve(input_tokens=1000, output_tokens=64, fingerprint=fingerprint)
    return budget, identifier, fingerprint


def test_remote_model_transport_requires_this_clients_reservation(tmp_path):
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hello"}]}
    budget, identifier, fingerprint = reservation(tmp_path, payload)
    client = object()
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=payload)
    with pytest.raises(RuntimeError, match="Unaccounted"):
        _check(client, request)
    with admitted_transport(object(), budget, identifier, fingerprint):
        with pytest.raises(RuntimeError, match="Unaccounted"):
            _check(client, request)
    with admitted_transport(client, budget, identifier, fingerprint):
        _check(client, request)
        with pytest.raises(BudgetExceeded):
            _check(client, request)
    assert budget.report()["provider_requests"] == 1
    assert budget.report()["unknown_requests"] == 1


@pytest.mark.parametrize(
    "url,model",
    [
        ("http://api.deepseek.com/chat/completions", "deepseek-flash"),
        ("https://api.deepseek.com:8443/chat/completions", "deepseek-flash"),
        ("https://other.example/chat/completions", "deepseek-flash"),
        ("https://api.deepseek.com/chat/completions", "deepseek-v4-pro"),
    ],
)
def test_reservation_cannot_pay_for_another_endpoint_or_model(tmp_path, url, model):
    payload = {"model": model, "messages": []}
    budget, identifier, fingerprint = reservation(tmp_path, payload)
    client = object()
    with admitted_transport(client, budget, identifier, fingerprint):
        with pytest.raises(RuntimeError, match="differs"):
            _check(client, httpx.Request("POST", url, json=payload))
    assert budget.report()["provider_requests"] == 0


def test_mutating_messages_cannot_reuse_an_admission(tmp_path):
    payload = {"model": "deepseek-flash", "messages": []}
    budget, identifier, fingerprint = reservation(tmp_path, payload)
    payload["messages"].append({"role": "user", "content": "larger unreserved request"})
    client = object()
    with admitted_transport(client, budget, identifier, fingerprint):
        with pytest.raises(RuntimeError, match="differs"):
            _check(
                client,
                httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=payload),
            )
    assert budget.report()["provider_requests"] == 0


def test_context_does_not_authorize_later_sends(tmp_path):
    payload = {"model": "deepseek-flash", "messages": []}
    budget, identifier, fingerprint = reservation(tmp_path, payload)
    client = object()
    with admitted_transport(client, budget, identifier, fingerprint):
        pass
    with pytest.raises(RuntimeError, match="Unaccounted"):
        _check(
            client, httpx.Request("POST", "https://api.deepseek.com/chat/completions", json=payload)
        )
