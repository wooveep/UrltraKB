"""Source-free document execution accounting and historical schema compatibility."""

from __future__ import annotations

import copy
import threading
from contextvars import copy_context

from openkb.agent.document_planning_events import planning_observation
from openkb.agent.shared_resources import SharedResourcePool
from openkb.execution_measurement import (
    Measurement,
    document_group_scope,
    measurement_scope,
    record_document_planning,
    record_document_totals,
    validate_measurement,
)


def test_shared_pool_reports_actual_atomic_peak_not_configured_capacity():
    measurement = Measurement()
    pool = SharedResourcePool(2, 500)
    entered = threading.Barrier(3)
    release = threading.Event()

    def request():
        with pool.admit(120, checkpoint=lambda: None):
            entered.wait(timeout=2)
            assert release.wait(2)

    with measurement_scope(measurement):
        threads = [threading.Thread(target=copy_context().run, args=(request,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        entered.wait(timeout=2)
        assert pool.current_inflight_tokens == 240
        assert pool.peak_inflight_tokens == 240
        release.set()
        for thread in threads:
            thread.join(2)
        assert all(not thread.is_alive() for thread in threads)
        assert pool.current_inflight_tokens == 0
    measurement.finalize()
    assert measurement.value["summary"]["peak_inflight_tokens"] == 240
    validate_measurement(measurement.value)


def test_group_usage_keeps_missing_provider_usage_unknown_and_reads_schema_three():
    measurement = Measurement()
    with measurement_scope(measurement), document_group_scope("private-group-id"):
        request = measurement.begin_request({"attempt": 1, "stage": "generation"}, 0.0, 0.0)
        measurement.finish_request(request)
    measurement.finalize()
    summary = measurement.value["summary"]
    assert summary["document"]["group_usage"][0]["input_tokens"] is None
    assert summary["document"]["group_usage"][0]["output_tokens"] is None
    assert summary["document"]["cost_usd"] is None
    validate_measurement(measurement.value)

    historic = copy.deepcopy(measurement.value)
    historic["schema"] = 3
    historic["summary"] = {
        key: value
        for key, value in summary.items()
        if key not in {"document", "peak_inflight_tokens"}
    }
    historic["requests"][0].pop("group")
    validate_measurement(historic)


def test_range_carry_counts_repeated_prior_ranges_not_new_target():
    measurement = Measurement()
    windows = [
        {"target_start": 0, "target_end": 1, "frozen_evidence_id": "w1"},
        {"target_start": 1, "target_end": 2, "frozen_evidence_id": "w2"},
    ]
    with measurement_scope(measurement):
        record_document_totals(evidence_groups=2, planned_pages=1)
        for index, window in enumerate(windows, start=1):
            record_document_planning(
                planning_observation(
                    "accepted",
                    window,
                    windows,
                    completed=index,
                    carry_pages=index - 1,
                    carry_unresolved=0,
                    pages=1,
                    unresolved=0,
                )
            )
    measurement.finalize()
    document = measurement.value["summary"]["document"]
    assert document["planning_calls"] == 2
    assert document["extra_planning_calls"] == 0
    assert document["range_carry_count"] == 1
    validate_measurement(measurement.value)
