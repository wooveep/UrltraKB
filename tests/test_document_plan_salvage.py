"""Bounded repair retains separately verifiable pages without filling missing source."""

from dataclasses import replace
from types import SimpleNamespace

from openkb.agent.document_plan_preview import planning_execution_metrics
from openkb.agent.document_plan_salvage import SalvagedPlan, salvage_candidate
from openkb.agent.document_planning_partial import prepare_partial_acceptance
from openkb.agent.document_windowing import (
    for_window_attempts,
    limit_split_attempts,
    split_planning_target,
)
from openkb.processing import DEFAULT_PROCESSING, RequestLimits
from tests.test_document_orchestrator import _DummyParsed
from tests.test_document_plan_compiler import _context


def test_malformed_recovered_planning_metrics_are_ignored():
    assert planning_execution_metrics({"planning_requests": "x", "elapsed_seconds": "x"}) == {
        "planning_requests": 0, "elapsed_seconds": 0.0,
    }
    invalid = {"planning_requests": True, "elapsed_seconds": float("nan")}
    assert planning_execution_metrics(invalid) == {
        "planning_requests": 0, "elapsed_seconds": 0.0,
    }
    assert planning_execution_metrics({"planning_requests": 2, "elapsed_seconds": 1.5}) == {
        "planning_requests": 2, "elapsed_seconds": 1.5,
    }


def test_salvage_and_blocked_page_have_independent_omissions(monkeypatch):
    monkeypatch.setattr(
        "openkb.agent.document_planning_partial.save_salvage_proof",
        lambda *_args: "proof",
    )
    ledger = SimpleNamespace(
        recovery_key="recovery", _meta=lambda _key: "identity",
        db=SimpleNamespace(execute=lambda *_args: []),
    )
    delta = {
        "page_changes": [{"target_key": "p1", "state": "blocked",
                          "subject_ranges": [[1, 2]]}],
        "unresolved": [{"status": "open", "blocking": True,
                        "affected_pages": ["p1"], "location": [[1, 2]]}],
        "resolutions": [],
    }
    salvaged = SalvagedPlan({}, delta, [[0, 1]], ["invalid"], [])

    partial = prepare_partial_acceptance(
        ledger, None, {"target_start": 0, "target_end": 2},
        original={}, candidate={}, delta=delta, salvaged=salvaged,
        attempts=1, normalizations=None,
    )

    assert len(partial.omissions) == 2
    invalid, blocked = partial.omissions
    assert invalid.reason == "document_plan_partial_invalid"
    assert invalid.ranges == [[0, 1]]
    assert invalid.affected_pages[0].startswith("omitted-page:")
    assert blocked.reason == "document_plan_blocked_page"
    assert blocked.ranges == [[1, 2]]
    assert blocked.affected_pages == ["p1"]




def test_bad_page_range_does_not_erase_an_independent_page():
    context = replace(_context(["First", "Second", "Metadata"]),
                      selection_protocol="document-plan-v5")
    ids = [block.id for block in context.parsed.blocks]

    def select(index):
        return {"from_block": ids[index], "through_block": ids[index]}

    candidate = {
        "overview": {"text": "Three source parts.", "ranges": [
            {"from_block": ids[0], "through_block": ids[2]}
        ], "limitations": []},
        "page_changes": [
            {"local_key": "valid", "kind": "concept", "title": "First",
             "purpose": "Explain first", "subject_ranges": [select(0)],
             "necessary_context": [], "limitations": []},
            {"local_key": "invalid", "kind": "concept", "title": "Second",
             "purpose": "Explain second", "subject_ranges": [
                 {"from_block": "unknown", "through_block": "unknown"}
             ], "necessary_context": [], "limitations": []},
        ],
        "source_only": [{"ranges": [select(2)], "reason": "Metadata retained."}],
        "unresolved": [], "resolutions": [], "external_references": [],
    }
    salvaged = salvage_candidate(candidate, context, {"target_start": 0, "target_end": 3})
    assert salvaged is not None
    assert [page["local_key"] for page in salvaged.delta["page_changes"]] == ["valid"]
    assert salvaged.affected_pages == ["invalid"]
    assert salvaged.omission_ranges
    assert "invalid" in [row["local_key"] for row in candidate["page_changes"]]


def test_bad_overview_range_keeps_independent_page_and_records_overview_gap():
    context = replace(_context(["First", "Metadata"]),
                      selection_protocol="document-plan-v5")
    ids = [block.id for block in context.parsed.blocks]

    def select(index):
        return {"from_block": ids[index], "through_block": ids[index]}
    candidate = {
        "overview": {"text": "Unsupported summary.", "ranges": [
            {"from_block": "unknown", "through_block": "unknown"}
        ], "limitations": []},
        "page_changes": [{
            "local_key": "valid", "kind": "concept", "title": "First",
            "purpose": "Explain first", "subject_ranges": [select(0)],
            "necessary_context": [], "limitations": [],
        }],
        "source_only": [{"ranges": [select(1)], "reason": "Metadata retained."}],
        "unresolved": [], "resolutions": [], "external_references": [],
    }
    salvaged = salvage_candidate(candidate, context, {"target_start": 0, "target_end": 2})
    assert salvaged is not None
    assert salvaged.component == "overview"
    assert salvaged.delta["overview"] == {"text": "", "ranges": [], "limitations": []}
    assert len(salvaged.delta["page_changes"]) == 1
    assert salvaged.omission_ranges == [[0, 2]]


def test_bad_external_annotation_is_reported_without_losing_page():
    context = replace(_context(["See 《Guide》."]), selection_protocol="document-plan-v5")
    identity = context.parsed.blocks[0].id
    selected = {"from_block": identity, "through_block": identity}
    candidate = {
        "overview": {"text": "Guide reference.", "ranges": [selected], "limitations": []},
        "page_changes": [{
            "local_key": "page", "kind": "concept", "title": "Guide",
            "purpose": "Record the source statement", "subject_ranges": [selected],
            "necessary_context": [], "limitations": [],
        }],
        "source_only": [], "unresolved": [], "resolutions": [],
        "external_references": [{
            "location": [selected], "target_document": "Other Guide", "target_section": None,
            "affected_pages": ["page"],
        }],
    }
    salvaged = salvage_candidate(candidate, context, {"target_start": 0, "target_end": 1})
    assert salvaged is not None
    assert salvaged.component == "reference_annotation"
    assert len(salvaged.delta["page_changes"]) == 1
    assert salvaged.delta["external_references"] == []
    assert salvaged.omission_ranges == [[0, 1]]


def test_output_split_divides_remaining_repairs_across_children():
    parsed = _DummyParsed(2)
    parent = {"target_start": 0, "target_end": 2}
    children = split_planning_target(parent, parsed)
    limit_split_attempts(children, parent, attempts_used=1, configured=3)
    assert [child["attempt_limit"] for child in children] == [2, 2]
    limits = RequestLimits.from_config({"model": "gpt-4o-mini", "processing": {
        **DEFAULT_PROCESSING, "max_attempts": 3,
    }})
    assert [for_window_attempts(limits, child, 3).max_attempts for child in children] == [2, 2]
    grandchildren = split_planning_target(children[0], parsed)
    limit_split_attempts(grandchildren, children[0], attempts_used=1, configured=3)
    assert [child["attempt_limit"] for child in grandchildren] == [2, 1]
