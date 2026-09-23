"""Formal document-plan and page-response failures remain bounded and recoverable."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("defect", ["missing", "duplicate", "wrong_id", "wrong_shape"])
def test_invalid_document_plan_response_retries_the_same_target(
    kb_dir, tmp_path, monkeypatch, defect
):
    source = tmp_path / "coverage.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.\n\nGamma requirement.")
    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            calls.append([block["text"] for block in payload["evidence"]["blocks"]])
            if len(calls) == 1:
                if defect == "missing":
                    value["page_changes"][0]["subject_ranges"] = [[0, 2]]
                elif defect == "duplicate":
                    value["page_changes"].append(dict(value["page_changes"][0]))
                elif defect == "wrong_id":
                    value["page_changes"][0]["target_key"] = "not-an-input-id"
                else:
                    value = {"page_changes": {}}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert result.usage["unknown_usage"] == 0


def test_invalid_plan_retry_reports_all_observed_contract_errors_without_changing_evidence(
    kb_dir, tmp_path, monkeypatch
):
    source = tmp_path / "retry-feedback.md"
    source.write_text("Recovery heading.\n\nRecovery step.")
    requests = []
    observed_codes = []
    events = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] != "planning":
            return response(value)
        requests.append(kwargs["messages"][-1]["content"])
        if len(requests) == 2:
            feedback = payload.get("retry_feedback", {})
            codes = {issue["code"] for issue in feedback.get("issues", [])}
            observed_codes.append(codes)
            if codes == {"invalid_range", "invalid_page_path", "nonblocking_unresolved"}:
                return response(value)
        value["page_changes"][0]["name"] = "concepts/恢复步骤"
        value["page_changes"][0]["necessary_context"] = [
            {
                "relation": "explicit_reference",
                "ranges": [[0, 0]],
                "basis": "Recovery heading",
                "basis_ranges": [[0, 1]],
            }
        ]
        value["unresolved"] = [
            {
                "location": [[0, 1]],
                "problem_type": "missing_external_material",
                "missing_target": "External instructions",
                "affected_pages": ["c1"],
                "blocking": False,
                "reason": "The referenced instructions were not supplied",
            }
        ]
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.knowledge_compilation == "completed", result
    assert not any(row["reason"] == "document_plan_invalid" for row in result.omissions), (
        observed_codes
    )
    assert len(requests) == 2
    first, second = requests
    assert first.partition(',"target":')[0] == second.partition(',"target":')[0]
    assert json.loads(first)["target"]["total_blocks"] == json.loads(first)["target"]["target_end"]
    feedback = json.loads(second)["retry_feedback"]
    assert feedback["total_blocks"] == json.loads(first)["target"]["total_blocks"]
    assert feedback["issue_counts"] == {
        "invalid_range": 1,
        "invalid_page_path": 1,
        "nonblocking_unresolved": 1,
    }
    assert len(feedback["issues"]) <= 12
    assert len(json.dumps(feedback)) <= 2000
    retry_event = next(row for row in events if row.get("operation") == "retry_invalid_response")
    assert {issue["field"] for issue in feedback["issues"]} == set(retry_event["invalid_fields"])


def test_persistent_invalid_document_plan_stops_after_bounded_retry(kb_dir, tmp_path, monkeypatch):
    source = tmp_path / "broken.md"
    source.write_text("A requirement.")
    calls, events = [], []

    def completion(**kwargs):
        calls.append(kwargs)
        return response({"page_changes": []})

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.status == "added"
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == "document_plan_invalid" for row in result.omissions)
    assert len(calls) == 2
    diagnostic = [event for event in events if event.get("operation") == "retry_invalid_response"]
    assert diagnostic[-1]["stage"] == "planning"
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_successful_planned_page_survives_later_generation_omission(kb_dir, tmp_path, monkeypatch):
    source = tmp_path / "partial.md"
    source.write_text("Good evidence.\n\nUnavailable evidence.")

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "planning":
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            blocks = payload["evidence"]["blocks"]
            good = next(block for block in blocks if block["text"].startswith("Good"))
            unavailable = next(block for block in blocks if block["text"].startswith("Unavailable"))
            value = {
                "overview": {
                    "text": "Two independent requirements.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "good",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/good",
                        "title": "Good",
                        "purpose": "Retain the available requirement.",
                        "subject_ranges": [[good["order"], good["order"] + 1]],
                        "necessary_context": [],
                    },
                    {
                        "local_key": "unavailable",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/unavailable",
                        "title": "Unavailable",
                        "purpose": "Retain the unavailable requirement.",
                        "subject_ranges": [[unavailable["order"], unavailable["order"] + 1]],
                        "necessary_context": [],
                    },
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif payload["stage"] == "generation" and payload["page"]["name"] == "concepts/unavailable":
            value = {"content": "", "covered": []}
        else:
            value = evidence_response(payload)
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"
    assert any(
        row["stage"] == "generation" and row["reason"] == "document_generation_incomplete"
        for row in result.omissions
    )
    assert (kb_dir / "wiki/concepts/good.md").is_file()
    assert not (kb_dir / "wiki/concepts/unavailable.md").exists()
