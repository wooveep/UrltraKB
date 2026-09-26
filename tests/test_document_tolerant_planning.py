"""Version four suggestions settle independently from later source retrieval."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.processing import InputTooLarge, ProcessingIncomplete
from tests.test_document_markdown_planning import SETTINGS, _parsed
from tests.test_document_orchestrator import _DummySource
from tests.test_document_planning_followups import _run_single


@pytest.mark.parametrize(
    "header", ["建议标题", "概念名称", "实体名称", "名称/标题", "Name / Title", "Suggested title"]
)
@pytest.mark.parametrize("clues", ["定位线索", "位置线索", "Location clues"])
def test_common_composite_headers_keep_named_suggestions_and_location_hints(header, clues):
    from openkb.agent.document_planning_response import accept_pages

    raw = (
        f"| {header} | 类型 | {clues} | 用途 / 说明 |\n|---|---|---|---|\n"
        "| Safe start | concept | Startup chapter | Describe startup |"
    )
    result = accept_pages(
        raw,
        navigation=[],
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.title == "Safe start" and page.purpose == "Describe startup"
    assert page.location_hints == [{"role": "related", "value": "Startup chapter"}]
    assert page.state == "pending_evidence" and not page.subject_ranges


def test_named_suggestions_and_dropped_rows_finish_without_candidate_retry(tmp_path, monkeypatch):
    result = _run_single(
        tmp_path,
        monkeypatch,
        [
            '[{"title":"Preparation"},{"title":"Unknown section","section":"section:absent"},'
            '{"kind":"concept","section":"section:absent"}]'
        ],
    )
    assert result.outcome == "complete"
    assert result.plan.metadata["protocol"] == "document-plan-v4"
    assert len(result.plan.pages) == 2
    assert all(page.state == "pending_evidence" for page in result.plan.pages)
    assert all(not page.subject_ranges for page in result.plan.pages)
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 2
    assert report["suggestions"]["dropped"] == 1
    assert not report["planning_omissions"]


def test_report_keeps_ocr_gaps_separate_from_usable_text(tmp_path, monkeypatch):
    parsed = _parsed()
    parsed.quality = [
        {"status": "verified", "reason": "docx_image_ocr_notice:page_budget_exhausted"}
    ]
    monkeypatch.setattr("tests.test_document_planning_followups._parsed", lambda: parsed)
    result = _run_single(tmp_path, monkeypatch, ["- Name: Preparation\n  Kind: concept"])
    report = json.loads(Path(result.report_ref).read_text())
    assert report["parser_gaps"] == report["planning_coverage"]["parser_gaps"] == 1


def test_suggestion_columns_allow_display_prefixes_and_parenthetical_labels(tmp_path, monkeypatch):
    result = _run_single(
        tmp_path,
        monkeypatch,
        [
            "| 建议页面 | 类型 | 建议位置线索（章节 / 关键词） | 用途说明 |\n"
            "|---|---|---|---|\n| Startup | concept | Startup chapter | Describe startup |"
        ],
    )
    assert len(result.plan.pages) == 1
    page = result.plan.pages[0]
    assert page.title == "Startup" and page.purpose == "Describe startup"
    assert page.location_hints == [{"role": "related", "value": "Startup chapter"}]


def test_global_budget_retains_overview_and_reports_unfinished_page_task(tmp_path, monkeypatch):
    class BudgetEnd:
        def __iter__(self):
            return self

        def __next__(self):
            raise ProcessingIncomplete("request_budget_exhausted", "planning")

    result = _run_single(tmp_path, monkeypatch, BudgetEnd())
    assert result.outcome == "budget_limited"
    assert result.overview_ref and result.plan.overview.text
    report = json.loads(Path(result.report_ref).read_text())
    assert report["outcome"] == "budget_limited"


def test_capacity_children_each_get_the_configured_attempts(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    counts = {}

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        if task["subtask"] == "overview":
            return "Retained overview."
        start, end = task["target"]["target_start"], task["target"]["target_end"]
        if end - start > 1:
            raise InputTooLarge()
        counts[start] = counts.get(start, 0) + 1
        return "" if counts[start] == 1 else f"- Name: Topic {start}\n  Kind: concept"

    settings = {**SETTINGS, "processing": {**SETTINGS["processing"], "max_attempts": 2}}
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
        report = json.loads(Path(result.report_ref).read_text())
        key = result.plan.metadata["recovery_key"]
        state = checkpoints.load_recovery(key, "markdown_plan")
        state["protocol"] = "document-planning-markdown-v1"
        checkpoints.save_recovery("f" * 64, "markdown_plan", state)
        (checkpoints.root / "recovery" / f"{key}-markdown_plan.json").unlink()

        def forbidden(*args, **kwargs):
            raise AssertionError("Retired parent overview must be retained")

        restored = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=forbidden,
            plan_only=True,
            return_result=True,
            resume=True,
            retry_skipped=True,
        )
        assert restored.plan.overview.text == result.plan.overview.text
        assert len(restored.plan.pages) == 2
    assert counts == {0: 2, 1: 2}
    assert len(result.plan.pages) == 2 and result.outcome == "complete"
    assert report["planning_execution"]["planning_requests"] == 5


def test_quote_after_operation_verb_is_not_a_document_reference():
    from openkb.agent.document_reference_check import detect_references

    parsed = _parsed()
    body = "按“Return”键继续，再按“确认”按钮；按“下一项”继续；参见《设备维护指南》。"
    parsed.blocks[0].text = body
    parsed.blocks[0].chars = len(body)
    evidence = {
        "parse_id": parsed.id,
        "blocks": [{"id": parsed.blocks[0].id, "order": 0, "text": body}],
    }
    refs = detect_references(evidence, None, {}, parsed)
    assert [row.target_text for row in refs] == ["设备维护指南"]


def test_continue_reextracts_old_raw_responses_before_spending_new_calls(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        return (
            "Overview retained."
            if task["subtask"] == "overview"
            else ("- Name: Operation\n  Section: section:unknown")
        )

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        first = plan_document(
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
        key = first.plan.metadata["recovery_key"]
        state = checkpoints.load_recovery(key, "markdown_plan")
        state["protocol"] = "document-planning-markdown-v1"
        checkpoints.save_recovery("e" * 64, "markdown_plan", state)
        (checkpoints.root / "recovery" / f"{key}-markdown_plan.json").unlink()

        def forbidden(*args, **kwargs):
            raise AssertionError("Historical usable responses need no model request")

        restored = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=forbidden,
            plan_only=True,
            return_result=True,
            resume=True,
            retry_skipped=True,
        )
    assert len(restored.plan.pages) == 1 and restored.outcome == "complete"
    assert restored.plan.pages[0].location_hints == [
        {"role": "subject", "value": "section:unknown"}
    ]
