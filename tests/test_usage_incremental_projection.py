"""Live usage stays responsive while durable receipts remain auditable."""

import pytest

from ultrakb import llm_usage as usage
from ultrakb.llm_usage_execution import import_usage_execution
from ultrakb.llm_usage_models import UsageReceipt


@pytest.mark.integration
def test_progress_does_not_scan_history_and_late_usage_is_counted_once(kb_dir, monkeypatch):
    events = []
    with import_usage_execution(kb_dir, "projection", on_event=events.append):
        identity = usage.begin_request("fixture", "summary")
        reads = []
        original = usage.read_requests

        def read(root):
            reads.append(root)
            return original(root)

        monkeypatch.setattr(usage, "read_requests", read)
        usage.finish_request(identity, state="cancelled", business_state="cancelled")
        response = {"id": "provider-1", "usage": {"prompt_tokens": 40, "completion_tokens": 8}}
        usage.finish_request(identity, response, send_status="sent", observation="transport")
        usage.finish_request(identity, response, send_status="sent", observation="transport")
        usage.finish_request(identity, {}, send_status="unknown", observation="call")
        assert reads == [], "A routine completion scanned the full request ledger"
        live = events[-1]["data"]
        UsageReceipt.model_validate(live)
        assert live["current"]["input_total"] == 40
        assert live["current"]["actual_sends"] == 1
        assert live["current"]["in_flight_requests"] == 0
        assert live["current"]["unknown_requests"] == 0
        final = usage.usage_receipt(kb_dir)
        assert final["current"]["request_ids"] == [identity]
        for field in ("input_total", "output_total", "requests", "actual_sends", "unknown_fields"):
            assert live["current"][field] == final["current"][field]


@pytest.mark.integration
def test_projection_rebuild_keeps_source_history_and_paid_receipts(kb_dir):
    from dataclasses import replace

    from ultrakb.usage_projection import progress_receipt

    source = "e" * 32
    with import_usage_execution(kb_dir, "first") as first:
        scope = replace(first, source_id=source)
        identity = usage.begin_request("fixture", "summary", scope=scope)
        usage.finish_request(
            identity,
            {"usage": {"prompt_tokens": 17, "completion_tokens": 3}},
            send_status="sent",
            observation="transport",
            scope=scope,
        )
        expected = progress_receipt(kb_dir, execution_ids=[first.execution_id], source_ids=[source])
    database = kb_dir / ".ultrakb/usage/projection.sqlite"
    database.write_bytes(b"interrupted projection storage")
    recovered = progress_receipt(kb_dir, execution_ids=[first.execution_id], source_ids=[source])
    assert recovered == expected
    assert usage.aggregate_usage(kb_dir)["request_ids"] == [identity]
    assert recovered["source_ids"] == [source]
    assert recovered["cumulative"]["input_total"] == 17


@pytest.mark.integration
def test_failed_projection_write_recovers_from_the_saved_paid_receipt(kb_dir, monkeypatch):
    import sqlite3

    from ultrakb.usage_projection import progress_receipt

    with import_usage_execution(kb_dir, "storage-failure") as scope:
        identity = usage.begin_request("fixture", "summary")

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("Projection storage unavailable")

        with monkeypatch.context() as fault:
            fault.setattr(sqlite3, "connect", unavailable)
            usage.finish_request(
                identity,
                {"usage": {"prompt_tokens": 23, "completion_tokens": 5}},
                send_status="sent",
                observation="transport",
                business_state="cancelled",
            )
        recovered = progress_receipt(kb_dir, execution_ids=[scope.execution_id])
        assert recovered["current"]["total_known"] == 28
        assert recovered["current"]["actual_sends"] == 1
        assert usage.aggregate_usage(kb_dir)["request_ids"] == [identity]


@pytest.mark.integration
def test_projection_intent_failure_cannot_discard_a_paid_receipt(kb_dir, monkeypatch):
    import os

    events = []
    with import_usage_execution(kb_dir, "intent-failure", on_event=events.append):
        identity = usage.begin_request("fixture", "summary")
        replace = os.replace

        def interrupted(source, target, *args, **kwargs):
            from pathlib import Path

            if Path(target).parent.name == "projection-pending":
                raise PermissionError("Projection intent directory unavailable")
            return replace(source, target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "replace", interrupted)
            usage.finish_request(
                identity,
                {"usage": {"prompt_tokens": 31, "completion_tokens": 7}},
                send_status="sent",
                observation="transport",
            )
        assert usage.aggregate_usage(kb_dir)["total_known"] == 38
        assert events[-1]["data"]["current"]["total_known"] == 38
