"""Global page selection happens after the available overview has settled."""

import json

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from tests.test_document_markdown_planning import SETTINGS
from tests.test_document_orchestrator import _DummyParsed, _DummySource


def run_global(tmp_path, respond, *, settings=None, resume=False, nodes=None):
    source, parsed = _DummySource(), _DummyParsed(3)
    for block, text in zip(
        parsed.blocks, ["Preparation requirements.", "Calibration steps.", "Recovery steps."]
    ):
        block.text, block.chars = text, len(text)
    navigation = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": nodes
        or [
            {
                "id": name.lower(),
                "title": name,
                "parent": None,
                "start": i,
                "end": i + 1,
                "summary": f"{name} for operating the instrument.",
            }
            for i, name in enumerate(["Preparation", "Calibration", "Recovery"])
        ],
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, i, i + 1),
                "target_start": i,
                "target_end": i + 1,
                "status": "complete",
                "reason": "",
                "target_tokens": 100,
            }
            for i in range(3)
        ],
    }
    settings = settings or {**SETTINGS, "model": "gpt-4o"}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True, exist_ok=True)
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


def test_three_source_windows_precede_one_global_page_selection(tmp_path):
    requests = []

    def respond(messages, *, settings):
        payload = json.loads(messages[-1]["content"])
        requests.append(payload)
        if payload["subtask"] == "overview":
            return f"Complete current overview {payload['target']['target_start']}."
        return "- Title: Instrument operation\n  Kind: concept\n  Section: Calibration"

    result = run_global(tmp_path, respond)
    assert [row["subtask"] for row in requests] == ["overview", "overview", "overview", "pages"]
    pages = requests[-1]
    assert "Complete current overview 2." in pages["planning_context"]["overview"]["text"]
    assert {row["title"] for row in pages["planning_context"]["topics"]} == {
        "Preparation",
        "Calibration",
        "Recovery",
    }
    assert {row["title"] for row in pages["navigation"]["hints"]} == {
        "Preparation",
        "Calibration",
        "Recovery",
    }
    assert pages["evidence"]["blocks"] == []
    assert result.plan.metadata["planning_strategy"] == "global-after-overview-v1"
    assert len(result.plan.pages) == 1


def test_small_real_budget_groups_branches_with_frozen_prefix_and_merges_additions(tmp_path):
    requests, prefixes = [], []
    nodes = []
    for i, name in enumerate(("Preparation", "Recovery")):
        nodes.append(
            {
                "id": name,
                "title": name,
                "parent": None,
                "start": i,
                "end": i + 1,
                "summary": "Branch overview.",
            }
        )
        for n in range(3):
            nodes.append(
                {
                    "id": f"{name}-{n}",
                    "title": f"{name} detail {n}",
                    "parent": name,
                    "start": i,
                    "end": i + 1,
                    "summary": "Detailed procedure context. " * 65,
                }
            )
    settings = {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {
            **SETTINGS["processing"],
            "context_tokens": 4200,
            "max_context_tokens": 4200,
            "output_tokens": 700,
            "max_output_tokens": 700,
        },
    }

    def respond(messages, *, settings):
        payload = json.loads(messages[-1]["content"])
        if payload["subtask"] == "overview":
            return "Instrument setup, preparation and recovery in the current overview."
        requests.append(payload)
        prefixes.append(messages[-1]["content"].split(',"plan_protocol"')[0])
        if len(requests) == 1:
            return "- Title: Instrument operation\n  Kind: concept\n  Section: Preparation"
        assert requests[-1]["carry"]["pages"][0]["title"] == "Instrument operation"
        return (
            "| Existing title | Additional location clues |\n|---|---|\n"
            "| Instrument operation | Recovery |"
        )

    result = run_global(tmp_path, respond, nodes=nodes, settings=settings)
    assert len(requests) == 2
    assert prefixes[0] == prefixes[1]
    assert result.plan.metadata["planning_mode"] == "topic_groups"
    assert [request["target"]["sections"] for request in requests] == [
        [f"section:{row['id']}" for row in nodes[:4]],
        [f"section:{row['id']}" for row in nodes[4:]],
    ]
    assert len(result.plan.pages) == 1
    assert any(hint["value"] == "Recovery" for hint in result.plan.pages[0].location_hints)


def test_resumption_does_not_repeat_settled_overviews_or_global_selection(tmp_path):
    calls = []

    def respond(messages, *, settings):
        calls.append(json.loads(messages[-1]["content"])["subtask"])
        return "Available overview." if calls[-1] == "overview" else "No new pages are warranted."

    first = run_global(tmp_path, respond)
    second = run_global(tmp_path, respond, resume=True)
    assert calls == ["overview", "overview", "overview", "pages"]
    assert first.plan.metadata["planning_snapshot"] == second.plan.metadata["planning_snapshot"]


def test_request_budget_reserves_global_pages_when_overview_cannot_finish(tmp_path):
    calls = []

    def respond(messages, *, settings):
        calls.append(json.loads(messages[-1]["content"])["subtask"])
        return (
            "Available partial overview."
            if calls[-1] == "overview"
            else "No new pages are warranted."
        )

    settings = {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {**SETTINGS["processing"], "max_requests": 2},
    }
    result = run_global(tmp_path, respond, settings=settings)
    assert calls == ["overview", "pages"]
    assert result.plan.metadata["planning_snapshot"]
    assert result.outcome == "budget_limited"
    report = json.loads(__import__("pathlib").Path(result.report_ref).read_text())
    assert report["planning_execution"]["pages_tasks"] == 1
    assert report["no_pages_recommended"]
    assert all(row["component"] == "overview" for row in report["planning_omissions"])


def test_truncated_global_selection_retains_pages_and_shares_child_attempt_budget(tmp_path):
    from openkb.agent.document_markdown_planner import _Response

    calls = []

    def respond(messages, *, settings):
        payload = json.loads(messages[-1]["content"])
        if payload["subtask"] == "overview":
            return "Overview of all operating stages."
        calls.append(payload)
        if len(calls) == 1:
            return _Response(
                "- Title: Operation\n  Kind: concept\n  Section: Preparation\n\nUnfinished",
                "length",
            )
        return ""

    result = run_global(tmp_path, respond)
    assert len(calls) == SETTINGS["processing"]["max_attempts"]
    assert len(result.plan.pages) == 1
    assert result.outcome == "partial"
    assert len({json.dumps(call["planning_context"], sort_keys=True) for call in calls}) == 1


def test_one_failed_topic_group_does_not_discard_an_accepted_group(tmp_path):
    from openkb.agent.document_markdown_planner import _Response

    calls = []

    def respond(messages, *, settings):
        payload = json.loads(messages[-1]["content"])
        if payload["subtask"] == "overview":
            return "Overview of all operating stages."
        calls.append(payload)
        if len(calls) == 1:
            return _Response("No new pages are warranted.\n\nUnfinished", "length")
        if len(calls) == 2:
            return "- Title: Preparation\n  Kind: concept\n  Section: Preparation"
        return ""

    result = run_global(tmp_path, respond)
    assert len(result.plan.pages) == 1 and result.plan.pages[0].title == "Preparation"
    assert result.outcome == "partial"
    report = json.loads(__import__("pathlib").Path(result.report_ref).read_text())
    assert (
        sum(
            task["status"] == "accepted"
            for task in report["tasks"].values()
            if task.get("component") == "pages"
        )
        == 1
    )


def test_physical_overview_retries_cannot_consume_the_reserved_pages_request():
    import pytest
    from openkb.processing import ProcessingIncomplete, processing_scope
    from openkb.processing_reservation import reserve_later_work

    settings = {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {**SETTINGS["processing"], "max_requests": 2},
    }
    request = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Overview"}]}
    with processing_scope(settings) as budget:
        with reserve_later_work(requests=1, tokens=100, attempts=3):
            budget.reserve(request)
            with pytest.raises(ProcessingIncomplete, match="pages_request_reserved"):
                budget.reserve(request)
        budget.reserve({**request, "messages": [{"role": "user", "content": "Pages"}]})
        assert budget.attempts == 2
