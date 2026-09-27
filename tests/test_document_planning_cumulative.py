"""Navigation overview provenance, partial artifacts and versioned recovery."""

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
        from openkb.agent.document_markdown_planner import _Response

        return (
            _Response(json.dumps(rows), "length")
            if not body["carry"]["pages"]
            else "无需新增页面。"
        )

    result = run_windows(tmp_path, respond, capacity=capacity)
    assert len(result.plan.pages) == 150
    carry = seen[-1]["carry"]
    assert {row["title"] for row in carry["pages"]} == {row["title"] for row in rows}
    assert carry["suggestions"]["total"] == 150
    assert carry["suggestions"]["details_clipped"] == (capacity == 8192)


def test_navigation_overview_is_reused_without_raw_window_receipts(tmp_path):
    seen = []

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        seen.append(body)
        return (
            "无需新增页面。"
            if body["subtask"] == "pages"
            else "# Manual\n\nCredentials and calibration."
        )

    result = run_windows(tmp_path, respond)
    snapshot = result.plan.metadata["overview_snapshot"]
    assert "processed" not in snapshot
    assert snapshot["node_keys"] == ["section:chapter"]
    assert snapshot["original_read_ranges"] == []
    assert len(result.plan.metadata["overview_history"]) == 1
    assert seen[1]["carry"]["overview"]["text"] == result.plan.overview.text
    assert seen[0]["planning_context"] == seen[1]["planning_context"]
    resumed = run_windows(tmp_path, respond, resume=True)
    assert len(seen) == 2
    assert resumed.plan.overview.text == result.plan.overview.text


def test_failed_overview_still_allows_pages_from_tree_summaries(tmp_path):
    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        return "" if body["subtask"] == "overview" else "- Title: Preparation\n  Kind: concept"

    result = run_windows(tmp_path, respond)
    assert result.outcome == "partial" and result.plan.overview.status == "partial"
    assert result.plan.metadata["overview_snapshot"] is None
    report = json.loads(Path(result.report_ref).read_text())
    assert len(report["overview"]["missing_tasks"]) == 1
    assert result.plan.pages[0].title == "Preparation"


def test_truncated_overview_keeps_closed_partial_artifact_without_full_snapshot(tmp_path):
    from openkb.agent.document_markdown_planner import _Response

    def respond(messages, *, settings):
        body = json.loads(messages[-1]["content"])
        return (
            "无需新增页面。"
            if body["subtask"] == "pages"
            else _Response("Readable paragraph.\n\nUnfinished", "length")
        )

    result = run_windows(tmp_path, respond)
    assert result.outcome == "partial"
    assert "Readable paragraph." in result.plan.overview.text
    assert "Unfinished" not in result.plan.overview.text
    assert result.plan.metadata["overview_snapshot"] is None
    assert result.plan.metadata["overview_parts"]


def test_legacy_window_state_cannot_supply_new_navigation_overview(tmp_path):
    from openkb.agent.document_planning_state import _state
    from openkb.sources import content_id

    settings = {**SETTINGS, "model": "gpt-4o"}
    with CompilationCheckpoints(tmp_path, _DummySource(), _parsed(), settings, None) as cp:
        old_key, new_key = content_id("legacy-strategy"), content_id("navigation-strategy")
        old = _state(cp, old_key, [], False)
        old.update(
            protocol="document-planning-acceptance-v4",
            planning_strategy="global-after-overview-v1",
            fragments={content_id("window"): "Legacy window fragment."},
        )
        cp.save_recovery(old_key, "markdown_plan", old)
        new = _state(cp, new_key, [], True)
        assert new["planning_strategy"] == "global-navigation-v2"
        assert new["overview_snapshot"] is None and new["tasks"] == {}
        assert cp.load_recovery(old_key, "markdown_plan") == old


@pytest.mark.parametrize(
    "broken", ["history", "snapshot", "input", "task", "task_kind", "part", "part_identity"]
)
def test_invalid_overview_history_is_rejected_at_the_resume_boundary(tmp_path, broken):
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
        if broken == "history":
            state["overview_history"] = [None]
        elif broken == "snapshot":
            state["overview_snapshot"].pop("text")
        elif broken == "input":
            state["overview_input"] = []
        elif broken == "task":
            state["overview_tasks"] = ["missing"]
        elif broken == "task_kind":
            state["tasks"][state["overview_tasks"][0]]["kind"] = []
        else:
            part = next(iter(state["overview_parts"].values()))
            if broken == "part":
                part["text"] = ["invalid"]
            else:
                part["sections"] = ["section:unrelated"]
        cp.save_recovery(key, "markdown_plan", state)
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
        run_windows(tmp_path, respond, settings=settings, resume=True)


def test_navigation_overview_rejects_fabricated_original_window_receipts(tmp_path):
    from openkb.processing import ProcessingIncomplete

    settings = {**SETTINGS, "model": "gpt-4o"}

    def respond(messages, *, settings):
        return (
            "A readable overview."
            if json.loads(messages[-1]["content"])["subtask"] == "overview"
            else "无需新增页面。"
        )

    result = run_windows(tmp_path, respond, settings=settings)
    with CompilationCheckpoints(tmp_path, _DummySource(), _parsed(), settings, None) as cp:
        key = result.plan.metadata["recovery_key"]
        state = cp.load_recovery(key, "markdown_plan")
        state["overview_snapshot"]["processed"] = [{"window": "a" * 64, "ranges": [[0, 2]]}]
        cp.save_recovery(key, "markdown_plan", state)
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
        run_windows(tmp_path, respond, settings=settings, resume=True)


def test_global_capacity_retry_does_not_split_or_repeat_overview(tmp_path, monkeypatch):
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
        if calls.count("pages") == 1:
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
