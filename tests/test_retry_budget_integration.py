"""Retry/budget intersections through real SDK requests to error-only HTTP."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from error_transport import ErrorTransport
from native_usage_fixtures import aggregate_usage, read_requests
from test_llm_error_transports import executor_for, request

from ultrakb.llm_execution import (
    AnswerReserveReached,
    ModelBudgetExceeded,
    ModelDeadlineExceeded,
    StageBudgetExceeded,
)
from ultrakb.locks import LockCancelled

pytestmark = pytest.mark.integration


def invoke(executor, *, asynchronous, role="answer"):
    if asynchronous:
        return asyncio.run(executor.acomplete(role, request()))
    return executor.complete(role, request())


def assert_failed_sends(kb_dir, count):
    records = read_requests(kb_dir)
    assert len(records) == count
    assert all(record.send_status == "sent" and record.state == "failed" for record in records)
    assert all(record.usage_status == "unknown" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    assert aggregate_usage(kb_dir)["actual_sends"] == count
    return records


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_http_retries_cannot_spend_the_reserved_final_answer_send(
    kb_dir, record_property, asynchronous
):
    with ErrorTransport(status=503) as service:
        executor = executor_for(service, kb_dir, max_calls=3, retries=3)
        executor.budget.preserve("answer_finish", 1)
        with pytest.raises(AnswerReserveReached):
            invoke(executor, asynchronous=asynchronous)
        assert len(service.requests) == executor.calls == 2
        assert [error.status_code for error in executor.model_errors[:2]] == [503, 503]
        assert executor.budget.snapshot()["reserved"] == {"answer_finish": 1}
        assert executor.budget.snapshot()["pending"] == 0
        before = assert_failed_sends(kb_dir, 2)
        assert len({record.logical_call_id for record in before}) == 1

        executor.budget.release("answer_finish")
        with pytest.raises(ModelBudgetExceeded):
            invoke(executor, asynchronous=asynchronous)
        assert len(service.requests) == executor.calls == 3
        assert executor.model_errors[-2].status_code == 503
        assert executor.budget.snapshot()["reserved"] == {}
        assert executor.budget.snapshot()["pending"] == 0
        record_property("http_sends", len(service.requests))
    records = assert_failed_sends(kb_dir, 3)
    before_ids = {record.request_attempt_id for record in before}
    [final_attempt] = [record for record in records if record.request_attempt_id not in before_ids]
    assert final_attempt.logical_call_id != before[0].logical_call_id


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stop_during_real_error_backoff_wins_over_the_exhausted_send_limit(
    kb_dir, record_property, asynchronous, stop
):
    with ErrorTransport(status=503) as service:
        deadline = time.monotonic() + 3
        executor = executor_for(
            service, kb_dir, max_calls=1, retries=3, backoff=5, deadline=deadline
        )
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(invoke, executor, asynchronous=asynchronous)
            observed_until = time.monotonic() + 2
            while not executor.model_errors and time.monotonic() < observed_until:
                threading.Event().wait(0.01)
            # Prove the SDK received HTTP 503 before stopping; a header gate or
            # socket close would exercise a different error path.
            assert executor.model_errors and executor.model_errors[0].status_code == 503
            assert len(service.requests) == executor.calls == 1
            assert not pending.done()
            if stop == "cancel":
                executor.cancel()
            expected = LockCancelled if stop == "cancel" else ModelDeadlineExceeded
            with pytest.raises(expected):
                pending.result(timeout=4)
        assert time.monotonic() < deadline + 1
        with pytest.raises(expected):
            invoke(executor, asynchronous=asynchronous)
        assert len(service.requests) == executor.calls == 1
        assert executor.budget.snapshot()["pending"] == 0
        record_property("http_sends", len(service.requests))
        record_property("sdk_error_status", executor.model_errors[0].status_code)
    assert_failed_sends(kb_dir, 1)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_real_retries_share_retrieval_allowance_and_next_question_keeps_task_consumption(
    kb_dir, record_property, asynchronous
):
    with ErrorTransport(status=503) as service:
        executor = executor_for(service, kb_dir, max_calls=11, retries=3)
        # Restore domain consumption, not a fabricated provider response or usage.
        executor.budget.restore(6, {"retrieval": 6})
        executor.budget.preserve("answer_finish", 1)
        with pytest.raises(StageBudgetExceeded):
            invoke(executor, asynchronous=asynchronous, role="knowledge_retrieval")
        assert len(service.requests) == 2 and executor.calls == 8
        assert executor.stage_calls == {"retrieval": 8}
        assert [error.status_code for error in executor.model_errors[:2]] == [503, 503]
        before = assert_failed_sends(kb_dir, 2)
        assert len({record.logical_call_id for record in before}) == 1

        executor.begin_question()
        assert executor.calls == 8 and executor.stage_calls == {}
        assert executor.budget.snapshot()["reserved"] == {"answer_finish": 1}
        with pytest.raises(AnswerReserveReached):
            invoke(executor, asynchronous=asynchronous, role="conversation_retrieval")
        assert len(service.requests) == 4 and executor.calls == 10
        assert executor.stage_calls == {"retrieval": 2}
        assert executor.budget.snapshot()["pending"] == 0
        assert executor.budget.snapshot()["reserved"] == {"answer_finish": 1}
        record_property("http_sends", len(service.requests))
        record_property("retained_sends", 6)
    records = assert_failed_sends(kb_dir, 4)
    assert len({record.logical_call_id for record in records}) == 2
