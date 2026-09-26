"""Observable settlement and reporting for best-effort planning."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from tests.test_document_markdown_planning import SETTINGS, _parsed
from tests.test_document_orchestrator import _DummySource


def test_window_containing_only_an_accepted_echo_settles_without_retry(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    navigation = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": [
            {"id": "first", "title": "前提", "parent": None, "start": 0, "end": 1},
            {"id": "second", "title": "操作", "parent": None, "start": 1, "end": 2},
        ],
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, start, end),
                "target_start": start,
                "target_end": end,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
            for start, end in ((0, 1), (1, 2))
        ],
    }
    calls = []

    def respond(messages, *, settings):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload["subtask"])
        if payload["subtask"] == "overview":
            return "This section preserves its original requirements."
        return "- Name: Preparation\n  Kind: concept\n  Section: section:first"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    assert result.outcome == "complete"
    assert calls == ["overview", "pages", "overview", "pages"]
    assert len(result.plan.pages) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert report["filtered_candidates"] == {"accepted_echo": 1}
    assert not report["planning_omissions"]


def _run_single(tmp_path, monkeypatch, responses):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    responses = iter(responses)

    def respond(messages, *, settings):
        if json.loads(messages[-1]["content"])["subtask"] == "overview":
            return "Credentials are required before the operation."
        return next(responses)

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        return plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )


def test_accepted_echo_does_not_erase_unresolved_rejected_candidate(tmp_path, monkeypatch):
    good = "- Name: Preparation\n  Kind: concept"
    bad = "- Name: Missing step\n  Kind: concept\n  Section: section:missing"
    result = _run_single(tmp_path, monkeypatch, [good + "\n" + bad, good, good])
    assert result.outcome == "partial"
    assert len(result.plan.pages) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 4
    assert report["rejected_candidates"][0]["reason"] == "unknown_location"
    assert report["planning_omissions"][0]["reason"] == "page_candidates_rejected"


def test_whole_source_page_does_not_hide_fallback_page_counts(tmp_path, monkeypatch):
    result = _run_single(
        tmp_path,
        monkeypatch,
        [
            json.dumps(
                [
                    {"name": "Manual", "kind": "concept", "subject_ranges": [[0, 2]]},
                    {"name": "Preparation", "kind": "concept"},
                    {"name": "Operation", "kind": "concept"},
                ]
            )
        ],
    )
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_coverage"]["precise_ratio"] == 1
    assert report["planning_coverage"]["fallback_chars"] == 0
    scopes = report["page_scopes"]
    assert scopes["by_resolution"] == {"section": 0, "explicit_range": 1, "target_fallback": 2}
    assert len(scopes["whole_source_pages"]) == 3
    assert result.plan.metadata["page_scopes"] == scopes
    preview = Path(report["plan_preview"]).read_text()
    assert "较宽范围页面：2" in preview
    assert "主体范围覆盖全部可读原文" in preview


def test_corrected_existing_page_can_merge_and_settle(tmp_path, monkeypatch):
    first = {"name": "Operation", "kind": "concept", "subject_ranges": [[0, 1]]}
    bad = {"name": "Operation", "kind": "concept", "section": "不存在"}
    corrected = {**first, "subject_ranges": [[1, 2]]}
    result = _run_single(tmp_path, monkeypatch, [json.dumps([first, bad]), json.dumps(corrected)])
    assert result.outcome == "complete"
    assert result.plan.pages[0].subject_ranges == [[0, 1], [1, 2]]
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 3
    assert not report["rejected_candidates"]
    assert len(report["rejected_candidate_history"]) == 1


def test_repeated_candidate_failures_report_actual_attempts_once(tmp_path, monkeypatch):
    bad = "- Name: Missing step\n  Kind: concept\n  Section: section:missing"
    result = _run_single(tmp_path, monkeypatch, [bad + "\n" + bad, bad, bad])
    assert result.outcome == "partial"
    report = json.loads(Path(result.report_ref).read_text())
    assert len(report["rejected_candidates"]) == 1
    assert report["rejected_candidates"][0]["attempts"] == 3
    assert len(report["rejected_candidate_history"]) == 4


@pytest.mark.parametrize(
    "bad_fields", [{"kind": "not-a-kind"}, {"kind": "entity", "type": "concept"}]
)
def test_correcting_candidate_kind_resolves_its_rejection(tmp_path, monkeypatch, bad_fields):
    page = {"name": "Operation", "kind": "concept", "subject_ranges": [[0, 1]]}
    result = _run_single(
        tmp_path, monkeypatch, [json.dumps({**page, **bad_fields}), json.dumps(page)]
    )
    assert result.outcome == "complete"
    report = json.loads(Path(result.report_ref).read_text())
    assert not report["rejected_candidates"]
    assert report["planning_execution"]["planning_requests"] == 3


def test_ambiguous_kind_cannot_be_resolved_by_two_same_name_categories(tmp_path, monkeypatch):
    page = {"name": "Operation", "kind": "concept", "subject_ranges": [[0, 1]]}
    ambiguous = {**page, "kind": "entity", "type": "concept"}
    two_categories = json.dumps([page, {**page, "kind": "entity", "type": "product"}])
    result = _run_single(
        tmp_path, monkeypatch, [json.dumps(ambiguous), two_categories, two_categories]
    )
    assert result.outcome == "partial"
    assert len(result.plan.pages) == 2
    report = json.loads(Path(result.report_ref).read_text())
    assert report["rejected_candidates"][0]["reason"] == "conflicting_kind"
