"""Bounded repair retains separately verifiable pages without filling missing source."""

import json
from dataclasses import replace

import litellm
import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_plan_salvage import salvage_candidate
from openkb.agent.document_planning_ledger import DocumentPlanningLedger
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_planning_support import fallback_read_evidence
from openkb.agent.document_windowing import (
    for_window_attempts,
    limit_split_attempts,
    split_planning_target,
)
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import DEFAULT_PROCESSING, RequestLimits, processing_scope
from openkb.sources import SourceStore
from tests.test_adaptive_processing import response
from tests.test_document_orchestrator import _DummyParsed, _DummySource
from tests.test_document_plan_compiler import _context


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


@pytest.mark.parametrize("window_ranges", [((0, 2),), ((0, 2), (2, 4))])
def test_partial_acceptance_survives_resume_and_retries_only_missing_range(
    tmp_path, monkeypatch, window_ranges
):
    source, parsed = _DummySource(), _DummyParsed(window_ranges[-1][1])
    windows = [
        {"evidence": evidence_descriptor(source, parsed, start, end),
         "target_start": start, "target_end": end, "status": "complete",
         "reason": "", "target_tokens": 1000}
        for start, end in window_ranges
    ]
    navigation = {
        "source_id": source.source_id, "version_id": source.id, "parse_id": parsed.id,
        "status": "complete", "windows": windows, "nodes": [],
    }
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    settings = {"model": "gpt-4o-mini", "processing": {
        **DEFAULT_PROCESSING, "context_tokens": 128_000, "max_context_tokens": 128_000,
        "max_attempts": 2,
    }}
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    calls = []
    retry = False

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload)
        if payload["response_mode"] == "patch":
            repair = payload["repair_request"]
            return response({
                "repair_protocol": "document-plan-repair-v2",
                "candidate_hash": repair["candidate_hash"], "operations": [],
            })
        blocks = payload["evidence"]["blocks"]
        target = payload["target"]
        start, end = target["target_start"], target["target_end"]

        def select(index):
            identity = next(row["id"] for row in blocks if row["order"] == index)
            return {"from_block": identity, "through_block": identity}

        if start == 0 and not retry:
            pages = [
                {"local_key": "valid", "kind": "concept", "title": "First",
                 "purpose": "Explain first", "subject_ranges": [select(0)],
                 "necessary_context": [], "limitations": []},
                {"local_key": "broken", "kind": "concept", "title": "Second",
                 "purpose": "Explain second", "subject_ranges": [select(1)],
                 "necessary_context": [], "limitations": [{
                     "ranges": [select(0)], "reason": "Wrong page evidence"
                 }]},
            ]
            selected = {"from_block": blocks[0]["id"], "through_block": blocks[1]["id"]}
            source_only = []
        else:
            pages = [{
                "local_key": f"page_{start}", "kind": "concept", "title": f"Part {start}",
                "purpose": f"Explain part {start}", "subject_ranges": [select(start)],
                "necessary_context": [], "limitations": [],
            }]
            selected = select(start)
            source_only = (
                [{"ranges": [select(start + 1)], "reason": "Retain metadata"}]
                if end - start > 1 else []
            )
        return response({
            "overview": {"text": f"Part {start} overview.", "ranges": [selected],
                         "limitations": []},
            "page_changes": pages, "source_only": source_only,
            "unresolved": [], "resolutions": [], "external_references": [],
        })

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            first = plan_document(
                tmp_path, workspace, source, parsed, navigation, settings, checkpoints,
                plan_only=True, return_result=True,
            )
    assert isinstance(first, PlanningResult) and first.outcome == "partial"
    assert first.plan is not None and len(first.plan.pages) == len(window_ranges)
    assert len(first.plan.planning_omissions) == 1
    assert first.plan.planning_omissions[0].ranges == [[1, 2]]
    before = len(calls)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            resumed = plan_document(
                tmp_path, workspace, source, parsed, navigation, settings, checkpoints,
                plan_only=True, return_result=True, resume=True,
            )
    assert isinstance(resumed, PlanningResult) and resumed.outcome == "partial"
    assert len(calls) == before
    retry = True
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            completed = plan_document(
                tmp_path, workspace, source, parsed, navigation, settings, checkpoints,
                plan_only=True, return_result=True, resume=True, retry_skipped=True,
            )
    assert isinstance(completed, PlanningResult) and completed.outcome == "complete"
    assert len(calls) == before + 1
    assert calls[-1]["target"]["target_start"] == 1
    assert calls[-1]["target"]["target_end"] == 2
    assert completed.plan is not None
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, completed.plan.metadata["recovery_key"])
        try:
            _, saved_windows, settled = ledger.progress()
            assert ledger.recovery_valid(
                saved_windows, settled, checkpoints.dispatch_output_tokens,
                parsed=parsed, source=source, base_windows=windows,
            )
            proof_key = ledger.receipt(1)["salvage_proof"]
            proof = SourceStore(tmp_path).root / "compilation" / f"{proof_key}.json"
            proof.unlink()
            assert not ledger.recovery_valid(
                saved_windows, settled, checkpoints.dispatch_output_tokens,
                parsed=parsed, source=source, base_windows=windows,
            )
        finally:
            ledger.close()
