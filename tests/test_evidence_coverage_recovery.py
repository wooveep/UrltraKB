"""Incomplete model coverage must not discard complete source evidence or stop a document."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("defect", ["missing", "duplicate", "wrong_id", "wrong_shape"])
def test_missing_unit_in_complete_response_recovers(kb_dir, tmp_path, monkeypatch, defect):
    source = tmp_path / "coverage.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.\n\nGamma requirement.")
    calls = []
    dropped = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            calls.append([unit["id"] for unit in payload["units"]])
            if len(calls) == 1:
                dropped.append(value["units"][-1]["id"])
                if defect == "missing":
                    value["units"].pop()
                elif defect == "duplicate":
                    value["units"][-1] = value["units"][0]
                elif defect == "wrong_id":
                    value["units"][-1]["id"] = "not-an-input-id"
                else:
                    value = {"units": {}}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert any(dropped[0] in ids for ids in calls[1:])
    assert result.usage["unknown_usage"] == 0


def test_persistent_single_unit_coverage_error_stops_after_bounded_retry(
    kb_dir, tmp_path, monkeypatch
):
    source = tmp_path / "broken.md"
    source.write_text("A requirement.")
    calls, events = [], []

    def completion(**kwargs):
        calls.append(kwargs)
        return response({"units": []})

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.knowledge_compilation == "unfinished"
    assert result.reason == "section_coverage_incomplete"
    assert len(calls) == 2
    diagnostic = [event for event in events if event.get("operation") == "response_invalid"]
    assert diagnostic[-1]["expected"] == 1 and diagnostic[-1]["missing"] == 1
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_partial_batch_progress_survives_a_later_unrecoverable_unit(kb_dir, tmp_path, monkeypatch):
    from openkb.progress import progress_reporting

    source = tmp_path / "partial.md"
    source.write_text("Good evidence.\n\nUnavailable evidence.")
    events = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            rejected = {unit["id"] for unit in payload["units"] if "Unavailable" in unit["text"]}
            value["units"] = [row for row in value["units"] if row["id"] not in rejected]
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    with progress_reporting(events.append):
        result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "unfinished"
    counters = [step for event in events for step in event["progress"] if step["phase"] == "facts"]
    assert counters[-1]["completed"] == len("Good evidence.")
    assert counters[-1]["completed"] < counters[-1]["total"]
