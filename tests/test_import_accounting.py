"""Import observations retain measured work without conflating it with provider billing."""

import json
import time

import pytest

from openkb.application.documents import DocumentResult, import_document
from openkb.application.source_history import source_status
from tests.http_model_fixture import evidence_response


def test_import_reports_stage_and_request_time_with_provider_cache_details(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "pressure.md"
    source.write_text("Required pressure is 37 kPa.")

    def respond(request):
        time.sleep(0.02)
        return evidence_response(json.loads(request["messages"][-1]["content"]))

    model_service.respond = respond
    model_service.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    measurement = result.usage["measurement"]
    assert {"parsing", "facts", "planning", "generation", "committing"} <= {
        span["stage"] for span in measurement["spans"]
    }
    requests = measurement["requests"]
    assert len(requests) == len(model_service) == 4
    assert len({row["id"] for row in requests}) == 4
    assert {row["operation"] for row in requests} == {
        "facts",
        "planning",
        "generation",
        "verification",
    }
    assert all(row["request_seconds"] >= 0.02 for row in requests)
    assert all(row["queue_seconds"] >= 0 for row in requests)
    assert all(row["cache_read_tokens"] == 40 for row in requests)
    assert all(row["cache_write_tokens"] is None for row in requests)
    assert all(row["input_tokens"] == 100 and row["cache_miss_tokens"] == 60 for row in requests)
    assert all(row["output_tokens"] == 30 and row["reasoning_tokens"] == 12 for row in requests)
    assert all(row["provider_model"] == "offline-test" for row in requests)
    assert all(row["effective_options"]["max_tokens"] == 1024 for row in requests)
    assert result.usage["charged_tokens"] == 520
    saved = source_status(kb_dir, result.source_id)["result"]
    assert saved["usage"]["measurement"] == measurement
    assert DocumentResult.from_summary(json.loads(json.dumps(saved))).usage == result.usage


def test_abandoned_optional_transport_has_bounded_wait_and_measured_lifetime():
    import threading

    from openkb.navigation_enhancement import IndexAllowance, IndexAllowanceExceeded
    from openkb.processing import ExecutionBudget, ProcessingIncomplete, RequestLimits
    from tests.test_adaptive_processing import profile

    settings = profile(request_timeout=0.15, concurrency=1)
    budget = ExecutionBudget(RequestLimits.from_config(settings))
    allowance = IndexAllowance(budget, profile(request_timeout=0.02), False)
    release = threading.Event()
    kwargs = {"model": "openai/test", "messages": [{"role": "user", "content": "JSON"}]}
    try:
        with allowance.enforce(), pytest.raises(IndexAllowanceExceeded):
            budget.call(lambda **options: release.wait(2), **kwargs)
        assert budget.measurement.active_requests == 1
        row = budget.measurement.value["requests"][0]
        assert row["transport_complete"] is False
        started = time.monotonic()
        with pytest.raises(ProcessingIncomplete, match="request_queue_timeout"):
            budget.call(lambda **options: pytest.fail("Outstanding transport owns slot"), **kwargs)
        assert time.monotonic() - started < 0.6
    finally:
        release.set()
    for _ in range(100):
        if budget.measurement.active_requests == 0:
            break
        time.sleep(0.01)
    assert budget.measurement.active_requests == 0
    assert row["transport_complete"] is True
    assert row["request_seconds"] >= 0.15
