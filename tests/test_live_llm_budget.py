"""Price arithmetic and admission checks; these tests never send model requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from llm_test_budget import BudgetExceeded, TestRunBudget


def test_full_run_reserves_every_send_before_admitting_it(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    first = budget.reserve(input_tokens=100_000, output_tokens=100_000)
    second = budget.reserve(input_tokens=100_000, output_tokens=100_000)
    assert budget.exposure_cny == Decimal("2")
    with pytest.raises(BudgetExceeded, match="2.00"):
        budget.reserve(input_tokens=1, output_tokens=1)
    assert budget.report()["requests"] == 2
    assert first != second


def test_provider_cache_usage_settles_reserved_cost(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    request = budget.reserve(input_tokens=100_000, output_tokens=1024)
    budget.settle(
        request,
        {
            "prompt_tokens": 100_000,
            "prompt_cache_hit_tokens": 60_000,
            "prompt_cache_miss_tokens": 40_000,
            "completion_tokens": 1000,
        },
        peak=True,
    )
    assert budget.actual_cny == Decimal("0.0904")
    assert budget.exposure_cny == budget.actual_cny
    assert budget.report()["usage"]["input_cache_hit_tokens"] == 60_000


def test_idle_and_peak_prices_are_applied_per_request(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    for peak in (False, True):
        request = budget.reserve(input_tokens=1000, output_tokens=1000)
        budget.settle(request, usage, peak=peak)
    assert budget.actual_cny == Decimal("0.015")
    assert budget.exposure_cny == Decimal("0.02")


def test_idle_discount_does_not_admit_a_suite_that_exceeds_the_peak_budget(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    request = budget.reserve(input_tokens=950_000, output_tokens=0)
    budget.settle(request, {"prompt_tokens": 950_000, "completion_tokens": 0}, peak=False)
    assert budget.actual_cny == Decimal("0.95")
    assert Decimal(budget.report()["all_peak_cny"]) == budget.exposure_cny == Decimal("1.9")
    budget.reserve(input_tokens=50_000, output_tokens=0)
    with pytest.raises(BudgetExceeded):
        budget.reserve(input_tokens=1, output_tokens=0)
    restored = TestRunBudget(budget.path)
    assert restored.report()["admission_basis"] == "peak_tariff"
    assert restored.exposure_cny == Decimal("2")


@pytest.mark.parametrize("legacy_guard", [None, False], ids=["missing", "false"])
def test_legacy_ledger_reports_prior_charges_but_cannot_admit_new_sends(tmp_path, legacy_guard):
    path = tmp_path / "cost.json"
    budget = TestRunBudget(path)
    identifier = budget.reserve(input_tokens=1000, output_tokens=1000)
    budget.settle(identifier, {"prompt_tokens": 1000, "completion_tokens": 1000}, peak=False)
    state = json.loads(path.read_text())
    if legacy_guard is None:
        state.pop("peak_guard")
    else:
        state["peak_guard"] = legacy_guard
    path.write_text(json.dumps(state))

    restored = TestRunBudget(path)
    prior_report = restored.report()
    assert prior_report["admission_basis"] == "request_tariff"
    assert prior_report["actual_cny"] == prior_report["exposure_cny"] == "0.005"
    assert prior_report["all_peak_cny"] == "0.01"
    with pytest.raises(BudgetExceeded, match="legacy ledger"):
        restored.reserve(input_tokens=1, output_tokens=1)
    assert json.loads(path.read_text()) == state
    assert restored.report() == prior_report


def test_peak_ledger_can_continue_without_changing_historical_charges(tmp_path):
    path = tmp_path / "cost.json"
    budget = TestRunBudget(path)
    identifier = budget.reserve(input_tokens=1000, output_tokens=1000)
    budget.settle(identifier, {"prompt_tokens": 1000, "completion_tokens": 1000}, peak=False)
    original_entry = json.loads(path.read_text())["entries"][identifier]

    restored = TestRunBudget(path)
    continued = restored.reserve(input_tokens=1000, output_tokens=64)
    state = json.loads(path.read_text())
    assert state["peak_guard"] is True
    assert state["entries"][identifier] == original_entry
    assert continued != identifier
    report = restored.report()
    assert report["admission_basis"] == "peak_tariff"
    assert report["actual_cny"] == "0.005"
    assert report["exposure_cny"] == "0.012512"
    assert report["unknown_requests"] == 1


@pytest.mark.parametrize("legacy_guard", [None, False], ids=["missing", "false"])
def test_legacy_pending_reservation_cannot_send_but_can_settle_prior_usage(tmp_path, legacy_guard):
    path = tmp_path / "cost.json"
    budget = TestRunBudget(path)
    identifier = budget.reserve(input_tokens=1000, output_tokens=1000, fingerprint="prior-request")
    state = json.loads(path.read_text())
    if legacy_guard is None:
        state.pop("peak_guard")
    else:
        state["peak_guard"] = legacy_guard
    path.write_text(json.dumps(state))

    restored = TestRunBudget(path)
    with pytest.raises(BudgetExceeded, match="legacy ledger"):
        restored.mark_sent(identifier, "prior-request")
    assert json.loads(path.read_text()) == state

    # A historical usage receipt remains a valid ledger input, without a new send.
    restored.settle(identifier, {"prompt_tokens": 1000, "completion_tokens": 1000}, peak=False)
    report = restored.report()
    assert report["admission_basis"] == "request_tariff"
    assert report["actual_cny"] == report["exposure_cny"] == "0.005"
    assert report["all_peak_cny"] == "0.01"
    assert report["requests"] == 1
    with pytest.raises(BudgetExceeded, match="legacy ledger"):
        restored.reserve(input_tokens=1, output_tokens=1)


def test_missing_usage_keeps_the_entire_reservation(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    request = budget.reserve(input_tokens=100_000, output_tokens=100_000)
    budget.uncertain(request, "connection_closed")
    assert budget.actual_cny == 0
    assert budget.exposure_cny == Decimal("1")
    assert budget.report()["unknown_requests"] == 1
    budget.reserve(input_tokens=100_000, output_tokens=100_000)
    with pytest.raises(BudgetExceeded):
        budget.reserve(input_tokens=1, output_tokens=1)


def test_concurrent_sends_cannot_overdraw_the_shared_budget(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")

    def reserve():
        try:
            return budget.reserve(input_tokens=100_000, output_tokens=50_000)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        admitted = list(workers.map(lambda _: reserve(), range(8)))
    assert len([request for request in admitted if request is not None]) == 3
    assert budget.exposure_cny == Decimal("1.8")


def test_ledger_survives_restart_and_settles_a_receipt_only_once(tmp_path):
    path = tmp_path / "cost.json"
    budget = TestRunBudget(path)
    request = budget.reserve(input_tokens=1000, output_tokens=1000)
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    budget.settle(request, usage, peak=True)
    restored = TestRunBudget(path)
    restored.settle(request, usage, peak=True)
    assert restored.actual_cny == Decimal("0.006")
    assert restored.report()["requests"] == 1


@pytest.mark.parametrize("limit", ["2.01", "0", "-1", "NaN", "Infinity"])
def test_hard_limit_cannot_be_disabled_or_raised(tmp_path, limit):
    with pytest.raises(ValueError):
        TestRunBudget(tmp_path / "cost.json", limit_cny=Decimal(limit))


def test_actual_receipt_cannot_silently_exceed_its_reservation(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    request = budget.reserve(input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetExceeded, match="reservation"):
        budget.settle(request, {"prompt_tokens": 1000, "completion_tokens": 1000}, peak=True)
    with pytest.raises(BudgetExceeded):
        budget.reserve(input_tokens=1, output_tokens=1)


def test_transport_cannot_reuse_or_change_a_reservation(tmp_path):
    budget = TestRunBudget(tmp_path / "cost.json")
    request = budget.reserve(input_tokens=100, output_tokens=64, fingerprint="actual-request")
    with pytest.raises(BudgetExceeded):
        budget.mark_sent(request, "different-request")
    budget.mark_sent(request, "actual-request")
    with pytest.raises(BudgetExceeded):
        budget.mark_sent(request, "actual-request")
    with pytest.raises(ValueError):
        budget.reject_before_send(request, "cannot refund a transport which left the process")


@pytest.mark.unit
def test_real_request_context_is_bounded_before_any_send():
    from live_request import MAX_CONTEXT_BYTES, provider_request

    with pytest.raises(ValueError, match="reduce its fixture"):
        provider_request(
            {"messages": [{"role": "user", "content": "汉" * (MAX_CONTEXT_BYTES // 3)}]}
        )
    payload, output_limit = provider_request(
        {"messages": [{"role": "user", "content": "Reply exactly ok."}], "max_tokens": 100_000},
        profile="bounded",
    )
    assert payload["messages"][0]["content"] == "Reply exactly ok."
    assert payload["max_tokens"] == output_limit == 2048


def test_directory_changes_cannot_fork_the_shared_run_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    budget = TestRunBudget(Path("test-run/cost.json"))
    budget.reserve(input_tokens=1000, output_tokens=64)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    budget.reserve(input_tokens=1000, output_tokens=64)
    assert budget.path == tmp_path / "test-run/cost.json"
    assert budget.report()["requests"] == 2
    assert not (other / "test-run").exists()


def test_billing_attribution_never_stores_fixture_parameter_text(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST", "tests/test_example.py::test_delivery[private-fixture] (call)"
    )
    budget = TestRunBudget(tmp_path / "cost.json")
    budget.reserve(input_tokens=100, output_tokens=64)
    stored = budget.path.read_text()
    assert json.loads(stored)["entries"][0]["test"] == "tests/test_example.py::test_delivery"
    assert "private-fixture" not in stored
