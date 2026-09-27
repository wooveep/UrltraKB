"""Version four suggestions settle independently from later source retrieval."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.processing import ProcessingIncomplete
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
    assert page.location_hints == [{"role": "subject", "value": "Startup chapter"}]
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
    assert not result.plan.pages
    deferred = result.plan.metadata["deferred_suggestions"]
    assert [row["title"] for row in deferred] == ["Preparation", "Unknown section"]
    assert all(row["reason"] == "classification_unknown" for row in deferred)
    assert deferred[1]["location_hints"] == [{"role": "subject", "value": "section:absent"}]
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["planning_requests"] == 2
    assert report["suggestions"]["dropped"] == 1
    assert report["suggestions"]["deferred"] == 2
    assert not report["no_pages_recommended"]
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
    assert page.location_hints == [{"role": "subject", "value": "Startup chapter"}]


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


def test_global_recovery_stops_at_the_shared_attempt_allowance(tmp_path, monkeypatch):
    calls = []

    def responses():
        while True:
            calls.append("pages")
            yield ""

    result = _run_single(tmp_path, monkeypatch, responses())
    assert len(calls) == SETTINGS["processing"]["max_attempts"]
    assert result.outcome == "partial" and result.plan.overview.text
    report = json.loads(Path(result.report_ref).read_text())
    assert report["planning_execution"]["pages_tasks"] == 1


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


def test_continue_keeps_legacy_pages_historical_and_requests_new_global_selection(
    tmp_path, monkeypatch
):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        return (
            "Overview retained."
            if task["subtask"] == "overview"
            else ("- Name: Operation\n  Kind: concept\n  Section: section:unknown")
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
        state.pop("overview_snapshot")
        state.pop("overview_history")
        state["fragments"] = {"a" * 64: "Legacy window overview."}
        checkpoints.save_recovery("e" * 64, "markdown_plan", state)
        (checkpoints.root / "recovery" / f"{key}-markdown_plan.json").unlink()

        calls = []

        def refresh(messages, *, settings):
            task = json.loads(messages[-1]["content"])
            calls.append(task["subtask"])
            return (
                "New cumulative overview."
                if task["subtask"] == "overview"
                else "- Name: Operation\n  Kind: concept\n  Section: section:unknown"
            )

        restored = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=refresh,
            plan_only=True,
            return_result=True,
            resume=True,
            retry_skipped=True,
        )
    assert len(restored.plan.pages) == 1 and restored.outcome == "complete"
    assert calls == ["overview", "pages"]
    assert restored.plan.overview.text.strip() == "New cumulative overview."
    assert restored.plan.pages[0].location_hints == [
        {"role": "subject", "value": "section:unknown"}
    ]
