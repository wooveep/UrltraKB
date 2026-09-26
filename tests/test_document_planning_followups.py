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


def test_one_chapter_contributes_only_each_window_then_merges_without_retries(
    tmp_path, monkeypatch
):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    navigation = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": [{"id": "chapter", "title": "操作章", "parent": None, "start": 0, "end": 2}],
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, start, start + 1),
                "target_start": start,
                "target_end": start + 1,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
            for start in (0, 1)
        ],
    }
    calls = []

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        calls.append((task["target"]["target_start"], task["subtask"], messages))
        if task["subtask"] == "overview":
            return "This window describes part of the chapter."
        return "- Name: Operation\n  Kind: concept\n  Section: section:chapter"

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
    assert result.outcome == "complete" and len(result.plan.pages) == 1
    assert result.plan.pages[0].subject_ranges == [[0, 2]]
    assert result.plan.pages[0].state == "pending_evidence"
    assert [(start, subtask) for start, subtask, _ in calls] == [
        (0, "overview"),
        (0, "pages"),
        (1, "overview"),
        (1, "pages"),
    ]
    for index in (0, 2):
        first, second = calls[index][2], calls[index + 1][2]
        assert first[0] == second[0]
        assert (
            first[-1]["content"].split('"plan_protocol":', 1)[0]
            == second[-1]["content"].split('"plan_protocol":', 1)[0]
        )


def _run_single(
    tmp_path, monkeypatch, responses, *, overview="Credentials are required before the operation."
):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    responses = iter(responses)

    def respond(messages, *, settings):
        if json.loads(messages[-1]["content"])["subtask"] == "overview":
            return overview
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


def test_partial_overview_is_not_completed_by_successful_page_planning(tmp_path, monkeypatch):
    from openkb.agent.document_markdown_planner import _Response

    result = _run_single(
        tmp_path,
        monkeypatch,
        ["- Name: Operation\n  Kind: concept"],
        overview=_Response("Saved paragraph.\n\nUnfinished", "length"),
    )
    assert result.outcome == "partial" and result.plan.overview.status == "partial"
    assert result.plan.pages and result.plan.overview.text.strip().endswith("Saved paragraph.")
    assert "局部资料" in result.plan.overview.text


def test_summary_only_response_settles_without_claiming_no_pages(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ["- Page: Manual overview\n  Type: Summary"])
    assert result.outcome == "complete"
    assert not result.plan.pages
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 2
    assert not report["no_pages_recommended"]
    assert report["filtered_candidates"] == {"summary_placeholder": 1}
    assert not report["planning_omissions"]


def test_ignored_summary_cannot_replace_a_failed_overview(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ["- Type: Summary"], overview="")
    assert result.outcome == "empty"
    assert result.plan is None and result.overview_ref is None
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 4
    assert [row["component"] for row in report["planning_omissions"]] == ["overview"]
    assert not report["no_pages_recommended"]


def test_saved_raw_page_response_resumes_without_new_requests(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def respond(messages, *, settings):
        subtask = json.loads(messages[-1]["content"])["subtask"]
        calls.append(subtask)
        return "Saved overview." if subtask == "overview" else "- Page: Operation\n  Kind: concept"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        save = checkpoints.save_recovery
        interrupted = False

        def interrupt_after_save(key, stage, value):
            nonlocal interrupted
            save(key, stage, value)
            if (
                stage == "markdown_plan"
                and not interrupted
                and any(
                    task.endswith(":pages") and record.get("raw")
                    for task, record in value["tasks"].items()
                )
            ):
                interrupted = True
                raise RuntimeError("interrupted after durable response")

        monkeypatch.setattr(checkpoints, "save_recovery", interrupt_after_save)
        with pytest.raises(RuntimeError, match="interrupted after durable response"):
            plan_document(
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
        for _ in range(2):
            result = plan_document(
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
                resume=True,
            )
            assert result.outcome == "complete" and len(result.plan.pages) == 1
            report = json.loads(Path(result.report_ref).read_text())
            assert report["planning_execution"]["planning_requests"] == 2
            assert not report["planning_omissions"]
    assert calls == ["overview", "pages"]
