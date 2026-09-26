"""Observable settlement and reporting for best-effort planning."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import InputTooLarge, ProcessingIncomplete
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
    assert result.plan.pages[0].subject_ranges == [[0, 1], [1, 2]]
    assert any("章节在本窗的片段" in note for note in result.plan.pages[0].planning_notes)
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


def test_accepted_echo_does_not_erase_unresolved_rejected_candidate(tmp_path, monkeypatch):
    good = "- Name: Preparation\n  Kind: concept"
    bad = "- Name: Missing step\n  Kind: concept\n  Section: section:missing"
    result = _run_single(tmp_path, monkeypatch, [good + "\n" + bad, good, good])
    assert result.outcome == "partial"
    assert result.plan.overview.status == "complete"
    assert len(result.plan.pages) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 4
    assert report["rejected_candidates"][0]["reason"] == "unknown_location"
    assert report["planning_omissions"][0]["reason"] == "page_candidates_rejected"


def test_partial_overview_is_not_completed_by_successful_page_planning(tmp_path, monkeypatch):
    from openkb.agent.document_markdown_planner import _Response

    result = _run_single(
        tmp_path,
        monkeypatch,
        ["- Name: Operation\n  Kind: concept"],
        overview=_Response("Saved paragraph.\n\nUnfinished", "length"),
    )
    assert result.outcome == "partial" and result.plan.overview.status == "partial"
    assert result.plan.pages and result.plan.overview.text.strip() == "Saved paragraph."


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
    "bad_fields",
    [
        {"kind": "not-a-kind"},
        {"kind": "entity", "type": "concept"},
        {"kind": "concept", "类别": "entity"},
    ],
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


@pytest.mark.parametrize("corrected_kind", ["concept", "entity"])
def test_entity_subtype_conflict_cannot_be_resolved_by_a_concept(
    tmp_path, monkeypatch, corrected_kind
):
    page = {"name": "Operation", "kind": "entity", "type": "product"}
    bad = {**page, "类型": "person"}
    corrected = {**page, "kind": corrected_kind}
    if corrected_kind == "concept":
        corrected.pop("type")
    result = _run_single(
        tmp_path, monkeypatch, [json.dumps(bad), json.dumps(corrected), json.dumps(corrected)]
    )
    report = json.loads(Path(result.report_ref).read_text())
    assert result.outcome == ("complete" if corrected_kind == "entity" else "partial")
    assert bool(report["rejected_candidates"]) == (corrected_kind == "concept")


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


def test_summary_only_response_settles_without_claiming_no_pages(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ["- Page: Manual overview\n  Type: Summary"])
    assert result.outcome == "complete"
    assert not result.plan.pages
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 2
    assert not report["no_pages_recommended"]
    assert report["filtered_candidates"] == {"summary_placeholder": 1}
    assert not report["planning_omissions"]


@pytest.mark.parametrize(
    "replacement",
    [
        "- Page: Manual overview\n  Type: Summary",
        "- Page: Manual overview\n  Kind: concept\n  Section: section:missing",
    ],
)
def test_ignore_resolves_only_the_corresponding_named_failure(tmp_path, monkeypatch, replacement):
    bad = "- Page: Manual overview\n  Type: unrecognized-kind"
    result = _run_single(tmp_path, monkeypatch, [bad, replacement, replacement])
    report = json.loads(Path(result.report_ref).read_text())
    if "Summary" in replacement:
        assert result.outcome == "complete"
        assert not report["planning_omissions"]
        assert report["rejected_candidate_history"][0]["resolution_reason"] == "summary_placeholder"
        assert report["rejected_candidate_history"][0]["resolved_attempt"] == 2
    else:
        assert result.outcome == "partial"
        assert report["rejected_candidates"]


@pytest.mark.parametrize("later", ["No new pages needed.", "- Page: Unrelated\n  Type: Summary"])
def test_no_pages_or_unrelated_ignore_cannot_clear_specific_failure(tmp_path, monkeypatch, later):
    bad = "- Page: Operation\n  Kind: concept\n  Section: section:missing"
    result = _run_single(tmp_path, monkeypatch, [bad, later, later])
    assert result.outcome == "partial"
    report = json.loads(Path(result.report_ref).read_text())
    assert len(report["rejected_candidates"]) == 1
    assert report["rejected_candidates"][0]["reason"] == "unknown_location"


def test_complete_ignored_response_resolves_only_response_level_failure(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ["", "- Type: Summary"])
    assert result.outcome == "complete"
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 3
    assert report["rejected_candidate_history"][0]["resolved"] is True
    assert not report["rejected_candidates"]


def test_ignored_summary_cannot_replace_a_failed_overview(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ["- Type: Summary"], overview="")
    assert result.outcome == "empty"
    assert result.plan is None and result.overview_ref is None
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 4
    assert [row["component"] for row in report["planning_omissions"]] == ["overview"]
    assert not report["no_pages_recommended"]


def test_opaque_failures_remain_distinct_after_unrelated_success(tmp_path, monkeypatch):
    bad = (
        "| Extra | Kind | Section |\n|---|---|---|\n"
        "| A | concept | 前提 |\n| B | concept | 操作 |\n| A | concept | 前提 |"
    )
    good = "- Page: Operation\n  Kind: concept"
    result = _run_single(tmp_path, monkeypatch, [bad, bad + "\n" + good, good])
    assert result.outcome == "partial" and len(result.plan.pages) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert len(report["rejected_candidate_history"]) == 6
    assert len(report["rejected_candidates"]) == 2
    assert all(row["attempts"] == 2 for row in report["rejected_candidates"])
    assert all(row["identity_kind"] == "opaque" for row in report["rejected_candidates"])


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


@pytest.mark.parametrize("child_name", ["Operation", "Unrelated topic"])
def test_split_parent_failure_remains_an_explicit_omission(tmp_path, monkeypatch, child_name):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        calls.append(task["subtask"])
        if task["subtask"] == "overview":
            return "Saved parent overview."
        if calls.count("pages") == 1:
            return "- Page: Operation\n  Kind: concept\n  Section: section:missing"
        if calls.count("pages") == 2:
            raise InputTooLarge()
        return f"- Page: {child_name}\n  Kind: concept"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        for resume in (False, True):
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
                resume=resume,
            )
            report = json.loads(Path(result.report_ref).read_text())
            assert result.outcome == "partial"
            assert (
                result.plan.pages and result.plan.overview.text.strip() == "Saved parent overview."
            )
            assert len(report["rejected_candidates"]) == 1
            pending = report["rejected_candidates"][0]
            assert pending["reason"] == "unknown_location" and pending["attempts"] == 1
            assert len(report["planning_omissions"]) == 1
            omission = report["planning_omissions"][0]
            assert omission["target_id"] == pending["window"]
            assert omission["ranges"] == [[0, 2]]
            assert omission["reason"] == "page_candidates_rejected"
            assert report["planning_execution"]["planning_requests"] == 4
        recovery_key = result.plan.metadata["recovery_key"]
        saved = checkpoints.load_recovery(recovery_key, "markdown_plan")
        for invalid in ([], "invalid-range", [[0, 3]]):
            altered = json.loads(json.dumps(saved))
            parent = altered["tasks"][pending["window"]]
            parent["retired_target"]["target_ranges"] = invalid
            checkpoints.save_recovery(recovery_key, "markdown_plan", altered)
            with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
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
                    resume=True,
                )
        checkpoints.save_recovery(recovery_key, "markdown_plan", saved)
    assert calls == ["overview", "pages", "pages", "pages", "pages"]
