"""Timing is observable at the model/storage seams without paid model requests."""

import threading
from concurrent.futures import ThreadPoolExecutor

import litellm
import pytest
from error_transport import ErrorTransport


@pytest.mark.integration
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_transport_observations_keep_queue_wait_separate_from_sent_request(kb_dir, asynchronous):
    import asyncio

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_timing import read_phase_events, timing_report
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        ModelRequest,
        RoleBindings,
    )
    from ultrakb.llm_usage import UsageScope, read_requests

    gate = threading.Event()
    with ErrorTransport(status=400, gate=gate) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "openai/gpt-4o-mini",
                LlmCredentialBundle(api_key="offline-test-key", base_url=service.url),
            ),
            ModelCallPolicy(concurrency=1, retries=0),
            scope=UsageScope(kb_dir, "a" * 32, source_id="b" * 32),
        )

        def invoke(work):
            request = ModelRequest(
                [{"role": "user", "content": "Only return a local HTTP error."}],
                operation="compile",
                stage=f"document-summary:{work}",
                generation_options={"max_tokens": 16},
            )
            try:
                if asynchronous:
                    asyncio.run(executor.acomplete("compile", request))
                else:
                    executor.complete("compile", request)
            except litellm.BadRequestError:
                pass

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(invoke, "1")
            assert service.wait_for_requests(1)
            second = workers.submit(invoke, "2")
            # The second work is observed while it is waiting for the shared slot.
            for _ in range(100):
                if sum(row.event == "queued" for row in read_phase_events(kb_dir)) == 2:
                    break
                threading.Event().wait(0.01)
            queued = [row for row in read_phase_events(kb_dir) if row.event == "queued"]
            assert len(queued) == 2
            assert len(service.requests) == 1
            gate.set()
            first.result(timeout=5)
            second.result(timeout=5)

    records = read_requests(kb_dir)
    events = read_phase_events(kb_dir)
    for work in ("1", "2"):
        rows = [row for row in events if row.work_id == f"document-summary:{work}"]
        assert [row.event for row in rows] == [
            "queued",
            "slot_acquired",
            "send",
            "response",
            "slot_released",
        ]
        assert len({row.logical_call_id for row in rows}) == 1
        assert all(row.source_id == "b" * 32 and row.execution_id == "a" * 32 for row in rows)
        assert all(row.occurred_at.endswith("+00:00") for row in rows)
        assert rows[1].duration_ns >= 0 and rows[3].duration_ns > 0
        assert rows[2].request_attempt_id == rows[3].request_attempt_id
        assert rows[3].outcome == "failed"
    assert {row.request_attempt_id for row in events if row.event == "send"} == {
        row.request_attempt_id for row in records
    }
    report = timing_report(kb_dir)
    assert report["actual_sends"] == 2
    assert report["peak_in_flight_requests"] == 1
    assert report["model_slot_limit"] == 1
    assert 0 < report["model_slot_utilization"] <= 1
    assert report["request_interval_union_seconds"] <= report["stage_wall_clock_seconds"]
    assert report["first_event_beijing"].endswith("+08:00")
    assert "offline-test-key" not in str(report)


def test_report_distinguishes_overlapping_request_time_from_phase_wall_time(kb_dir, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import ultrakb.execution_timing as timing
    from ultrakb.llm_usage import UsageScope

    now = [0]
    wall_adjustment = [0]

    class Clock(datetime):
        @staticmethod
        def now(_timezone):
            return datetime(2026, 10, 18, tzinfo=timezone.utc) + timedelta(
                seconds=now[0] + wall_adjustment[0]
            )

    monkeypatch.setattr(timing, "datetime", Clock)
    monkeypatch.setattr(timing.time, "monotonic_ns", lambda: int(now[0] * 1e9))
    scope = UsageScope(kb_dir, "a" * 32)
    correlation = {"scope": scope, "stage": "compile.document-summary:1", "work_id": "1"}
    timing.record_phase_event("queued", **correlation)
    now[0] = 1
    first = timing.start_observation()
    timing.record_phase_event("send", request_attempt_id="b" * 32, **correlation)
    now[0] = 2
    second = timing.start_observation()
    timing.record_phase_event("send", request_attempt_id="c" * 32, **correlation)
    now[0] = 5
    timing.record_phase_event("response", request_attempt_id="b" * 32, started=first, **correlation)
    now[0] = 6
    timing.record_phase_event(
        "response", request_attempt_id="c" * 32, started=second, **correlation
    )
    # A wall-clock adjustment must not alter measured local phase durations.
    with timing.phase_observation("validation", **correlation):
        now[0] = 9
        wall_adjustment[0] = -3600

    monkeypatch.setattr(timing, "datetime", datetime)
    report = timing.timing_report(kb_dir)
    assert report["concurrent_request_seconds_sum"] == 8
    assert report["request_interval_union_seconds"] == 5
    assert report["stage_wall_clock_seconds"] == 9
    assert report["peak_in_flight_requests"] == 2
    assert report["interval_clock"] == "monotonic"
    [validation] = [row for row in timing.read_phase_events(kb_dir) if row.event == "validation"]
    assert validation.duration_ns == 3_000_000_000


def test_historical_execution_does_not_invent_missing_phase_times(kb_dir):
    from ultrakb.execution_timing import timing_report

    report = timing_report(kb_dir)
    assert report["observation_status"] == "unrecorded"
    assert report["stage_wall_clock_seconds"] is None
    assert report["request_interval_union_seconds"] is None
    assert report["first_event_beijing"] is None


@pytest.mark.integration
def test_transport_timeout_has_a_settlement_without_an_invented_response(kb_dir):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_timing import read_phase_events, timing_report
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        ModelRequest,
        RoleBindings,
    )
    from ultrakb.llm_usage import UsageScope, read_requests

    with ErrorTransport(status=504, gate=threading.Event()) as service:
        executor = CompletionExecutor(
            RoleBindings(
                "openai/gpt-4o-mini",
                LlmCredentialBundle(api_key="offline-test-key", base_url=service.url, timeout=0.05),
            ),
            ModelCallPolicy(retries=0),
            scope=UsageScope(kb_dir, "a" * 32),
        )
        with pytest.raises(litellm.Timeout):
            executor.complete(
                "compile",
                ModelRequest(
                    [{"role": "user", "content": "Local timeout."}],
                    operation="compile",
                    stage="summary",
                ),
            )
    events = read_phase_events(kb_dir)
    assert not any(row.event == "response" for row in events)
    [settled] = [row for row in events if row.event == "request_settled"]
    [request] = read_requests(kb_dir)
    assert settled.request_attempt_id == request.request_attempt_id
    assert settled.outcome == "failed" and request.usage_status == "unknown"
    report = timing_report(kb_dir)
    assert report["settled_requests"] == 1
    assert report["observed_responses"] == 0


def test_phase_observation_keeps_task_and_failed_validation_outcome(kb_dir):
    from ultrakb.execution_timing import phase_observation, read_phase_events, timing_report
    from ultrakb.llm_usage_execution import import_usage_execution

    with import_usage_execution(kb_dir, "fixture", task_id="a" * 32) as scope:
        with pytest.raises(ValueError, match="Invalid accepted result"):
            with phase_observation("validation", stage="compile.summary", work_id="window:1"):
                raise ValueError("Invalid accepted result")
    [event] = read_phase_events(kb_dir)
    assert event.task_id == "a" * 32 and event.execution_id == scope.execution_id
    assert event.outcome == "failed" and event.duration_ns is not None
    assert timing_report(kb_dir)["execution_tasks"] == {scope.execution_id: "a" * 32}


def test_observation_storage_failure_preserves_the_business_error(kb_dir, monkeypatch):
    from ultrakb.execution_timing import phase_observation, read_phase_events
    from ultrakb.llm_usage import UsageScope

    def unavailable(*args, **kwargs):
        raise OSError("Observation storage unavailable")

    monkeypatch.setattr("ultrakb.execution_timing.atomic_write_json", unavailable)
    with pytest.raises(ValueError, match="Paid result validation failed"):
        with phase_observation(
            "validation", stage="compile.summary", scope=UsageScope(kb_dir, "a" * 32)
        ):
            raise ValueError("Paid result validation failed")
    assert not read_phase_events(kb_dir)


@pytest.mark.integration
def test_late_transport_receipt_cannot_write_into_a_recreated_knowledge_base(kb_dir):
    import httpx

    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.execution_timing import read_phase_events, record_phase_event
    from ultrakb.kb_admin import delete_kb
    from ultrakb.lifecycle import KnowledgeBaseRemoved, current_generation
    from ultrakb.llm_usage import begin_request, read_requests
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.llm_usage_transport import observe_model_call

    progress = []
    with (
        ErrorTransport(status=400) as service,
        import_usage_execution(kb_dir, "fixture", on_event=progress.append) as scope,
    ):
        with observe_model_call("openai/gpt-4o-mini", "compile.summary") as call:
            response = httpx.post(
                service.url + "/chat/completions",
                json={"messages": [{"role": "user", "content": "Local error only."}]},
            )
            [request_id] = call.sent_ids
        with observe_model_call("openai/gpt-4o-mini", "compile.summary") as unsent:
            pass
        assert read_requests(kb_dir)

    delete_kb(kb_dir)
    initialize_kb(kb_dir, seed_environment=False, require_empty=True)
    assert current_generation(kb_dir) != scope.generation
    assert not read_requests(kb_dir)
    before = {
        path.relative_to(kb_dir): path.read_bytes() for path in kb_dir.rglob("*") if path.is_file()
    }
    progress.clear()
    # The retained response from the real loopback send reaches its observer again.
    call.received(request_id, response)
    record_phase_event(
        "response", stage="compile.summary", scope=scope, request_attempt_id=request_id
    )
    with pytest.raises(KnowledgeBaseRemoved):
        begin_request("openai/gpt-4o-mini", "compile.summary", scope=scope)
    with pytest.raises(KnowledgeBaseRemoved):
        unsent.sending()
    assert not read_requests(kb_dir)
    assert not read_phase_events(kb_dir)
    assert not progress
    after = {
        path.relative_to(kb_dir): path.read_bytes() for path in kb_dir.rglob("*") if path.is_file()
    }
    assert after == before
