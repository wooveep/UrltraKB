"""Cumulative information remains observable through the planning entry point."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from tests.test_document_markdown_planning import SETTINGS, _parsed
from tests.test_document_orchestrator import _DummySource


def run_windows(tmp_path, respond, *, capacity=128000, resume=False, settings=None):
    source, parsed = _DummySource(), _parsed()
    parsed.blocks[1].text = "Preparation credentials are required for calibration."
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True, exist_ok=True)
    settings = settings or {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {
            **SETTINGS["processing"],
            "context_tokens": capacity,
            "max_context_tokens": capacity,
            "output_tokens": 1024,
            "max_output_tokens": 1024,
        },
    }
    navigation = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": [{"id": "chapter", "title": "Preparation", "parent": None, "start": 0, "end": 2}],
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
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        return plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
            resume=resume,
        )


@pytest.mark.parametrize("capacity", [128000, 8192])
def test_catalog_keeps_early_relevant_suggestion_with_honest_budget_projection(tmp_path, capacity):
    seen = []
    rows = [{"title": "Preparation", "kind": "concept", "purpose": "Prepare credentials."}]
    rows += [
        {
            "title": f"Topic {i}",
            "kind": "concept",
            "purpose": "A distinct subject about " + "details " * 100 + ".",
        }
        for i in range(149)
    ]

    def respond(messages, *, settings):
        import litellm

        body = json.loads(messages[-1]["content"])
        assert litellm.token_counter(model=settings["model"], messages=messages) <= capacity - 1024
        seen.append(body)
        if body["subtask"] == "overview":
            return "# Manual\n\nA cumulative description of prerequisites and calibration."
        return json.dumps(rows) if body["target"]["target_start"] == 0 else "无需新增页面。"

    result = run_windows(tmp_path, respond, capacity=capacity)
    assert len(result.plan.pages) == 150
    carry = seen[-1]["carry"]
    assert carry["suggestions"]["total"] == 150
    assert "Preparation" in [row["title"] for row in carry["pages"]]
    assert carry["suggestions"]["shown"] == len(carry["pages"]) + len(carry["other_titles"])
    assert carry["suggestions"]["omitted"] == (carry["suggestions"]["shown"] < 150)
    if capacity == 128000:
        assert len(carry["pages"]) == 150 and not carry["other_titles"]
    else:
        assert carry["other_titles"]


def test_overview_replaces_prior_snapshot_and_resume_does_not_repeat_calls(tmp_path):
    seen = []

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        seen.append(body)
        if body["subtask"] == "pages":
            return "无需新增页面。"
        return (
            "# Manual\n\nInitial credentials."
            if body["target"]["target_start"] == 0
            else "# Manual\n\nCredentials and calibration form the complete workflow."
        )

    result = run_windows(tmp_path, respond)
    assert (
        result.plan.overview.text.strip()
        == "# Manual\n\nCredentials and calibration form the complete workflow."
    )
    assert seen[2]["carry"]["overview"] == "# Manual\n\nInitial credentials."
    snapshot = result.plan.metadata["overview_snapshot"]
    assert len(snapshot["processed"]) == 2 and snapshot["response"]
    assert len(result.plan.metadata["overview_history"]) == 2
    resumed = run_windows(tmp_path, respond, resume=True)
    assert len(seen) == 4
    assert resumed.plan.overview.text == result.plan.overview.text


def test_failed_first_window_remains_missing_after_later_overview_succeeds(tmp_path):
    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        if body["subtask"] == "pages":
            return "无需新增页面。"
        return (
            ""
            if body["target"]["target_start"] == 0
            else "# Manual\n\nCalibration; the earlier source has not been summarized."
        )

    result = run_windows(tmp_path, respond)
    assert result.outcome == "partial" and result.plan.overview.status == "partial"
    assert len(result.plan.metadata["overview_snapshot"]["processed"]) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert len(report["overview"]["missing_windows"]) == 1


def test_truncated_later_overview_keeps_previous_complete_snapshot(tmp_path):
    from openkb.agent.document_markdown_planner import _Response

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        if body["subtask"] == "pages":
            return "无需新增页面。"
        return (
            "Complete prior overview."
            if body["target"]["target_start"] == 0
            else _Response("Incomplete newer paragraph.\n\nUnfinished", "length")
        )

    result = run_windows(tmp_path, respond)
    assert result.outcome == "partial"
    assert result.plan.overview.text.strip() == "Complete prior overview."
    assert len(result.plan.metadata["overview_snapshot"]["processed"]) == 1


def test_legacy_window_overviews_require_new_cumulative_tasks_but_reuse_pages(tmp_path):
    seen = []

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        seen.append(body["subtask"])
        return "New cumulative overview." if body["subtask"] == "overview" else "无需新增页面。"

    settings = {**SETTINGS, "model": "gpt-4o"}
    initial = run_windows(tmp_path, respond, settings=settings)
    from openkb.agent.document_window_receipts import window_receipt_id

    with CompilationCheckpoints(tmp_path, _DummySource(), _parsed(), settings, None) as cp:
        key = initial.plan.metadata["recovery_key"]
        old = cp.load_recovery(key, "markdown_plan")
        old["protocol"] = "document-planning-acceptance-v2"
        old.pop("overview_snapshot")
        old.pop("overview_history")
        old["fragments"] = {window_receipt_id(old["windows"][0]): "Legacy window fragment."}
        cp.save_recovery(key, "markdown_plan", old)
    resumed = run_windows(tmp_path, respond, settings=settings, resume=True)
    assert seen == ["overview", "pages", "overview", "pages", "overview", "overview"]
    assert "Legacy" not in resumed.plan.overview.text
    assert len(resumed.plan.metadata["overview_snapshot"]["processed"]) == 2


def test_invalid_overview_history_is_rejected_at_the_resume_boundary(tmp_path):
    from openkb.processing import ProcessingIncomplete

    def respond(messages, *, settings):
        return (
            "A readable overview."
            if json.loads(messages[-1]["content"])["subtask"] == "overview"
            else "无需新增页面。"
        )

    settings = {**SETTINGS, "model": "gpt-4o"}
    result = run_windows(tmp_path, respond, settings=settings)
    with CompilationCheckpoints(tmp_path, _DummySource(), _parsed(), settings, None) as cp:
        key = result.plan.metadata["recovery_key"]
        state = cp.load_recovery(key, "markdown_plan")
        state["overview_history"] = [None]
        cp.save_recovery(key, "markdown_plan", state)
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
        run_windows(tmp_path, respond, settings=settings, resume=True)


def test_large_processed_history_is_projected_before_source_is_split(tmp_path):
    import litellm

    settings = {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {
            **SETTINGS["processing"],
            "context_tokens": 8192,
            "max_context_tokens": 8192,
            "output_tokens": 1024,
            "max_output_tokens": 1024,
        },
    }
    seen = []

    def respond(messages, *, settings):
        assert litellm.token_counter(model=settings["model"], messages=messages) <= 7168
        body = json.loads(messages[-1]["content"])
        seen.append(body)
        return (
            "A readable complete overview." if body["subtask"] == "overview" else "无需新增页面。"
        )

    result = run_windows(tmp_path, respond, settings=settings)
    with CompilationCheckpoints(tmp_path, _DummySource(), _parsed(), settings, None) as cp:
        key = result.plan.metadata["recovery_key"]
        state = cp.load_recovery(key, "markdown_plan")
        state["overview_snapshot"]["processed"] = [
            {"window": str(i).zfill(64), "ranges": [[0, 1]]} for i in range(2000)
        ]
        for task in state["tasks"].values():
            task.update(status="pending", attempts=0)
        cp.save_recovery(key, "markdown_plan", state)
    run_windows(tmp_path, respond, settings=settings, resume=True)
    assert len(seen) == 8
    assert seen[4]["carry"]["processed_overview"]["omitted"]


def test_nested_capacity_splits_inherit_the_original_overview_input(tmp_path, monkeypatch):
    from openkb.processing import InputTooLarge
    from tests.test_document_orchestrator import _DummyParsed

    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    source, parsed = _DummySource(), _DummyParsed(4)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    calls = []

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        calls.append(body["subtask"])
        if body["subtask"] == "overview":
            return "The entire source describes a procedure."
        if body["target"]["target_end"] - body["target"]["target_start"] > 1:
            raise InputTooLarge()
        return "无需新增页面。"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as cp:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            cp,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    assert result.outcome == "complete" and calls.count("overview") == 1
