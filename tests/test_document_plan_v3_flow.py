"""Small real-dispatch planning tests for v4 compilation and scoped repairs."""

import json
from copy import deepcopy

import litellm
import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_plan_repair_state import V3PlanningRepairSession
from openkb.agent.document_planning_support import fallback_read_evidence
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.processing import DEFAULT_PROCESSING, ProcessingIncomplete, processing_scope
from tests.test_adaptive_processing import response
from tests.test_document_orchestrator import _DummyParsed, _DummySource, _two_window_navigation

SETTINGS = {
    "model": "mock-model",
    "processing": {
        **DEFAULT_PROCESSING,
        "context_tokens": 128_000,
        "max_context_tokens": 128_000,
        "output_tokens": 4_096,
        "max_output_tokens": 4_096,
        "max_attempts": 2,
    },
}


def _candidate():
    return {
        "overview": {"text": "两段资料。", "ranges": [[0, 2]], "limitations": []},
        "page_changes": [
            {
                "local_key": "c1",
                "kind": "concept",
                "title": "执行流程",
                "purpose": "说明流程和条件",
                "subject_ranges": [[1, 2]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }


def _selection(payload, first, last=None):
    blocks = payload["evidence"]["blocks"]
    by_order = {block["order"]: block["id"] for block in blocks}
    return {
        "from_block": by_order[first],
        "through_block": by_order[first if last is None else last],
    }


def _wire_ranges(payload, candidate):
    """Turn this fixture's numeric ranges into request-local model selections."""
    candidate = deepcopy(candidate)

    def convert(ranges):
        return [
            _selection(payload, value[0], value[1] - 1) if isinstance(value, list) else value
            for value in ranges
        ]

    candidate["overview"]["ranges"] = convert(candidate["overview"]["ranges"])
    for page in candidate["page_changes"]:
        page["subject_ranges"] = convert(page["subject_ranges"])
    for row in candidate["source_only"]:
        row["ranges"] = convert(row["ranges"])
    return candidate


@pytest.mark.parametrize("route", ["page_body", "source_only", "new_page"])
def test_real_planning_dispatch_routes_gap(tmp_path, monkeypatch, route):
    source, parsed = _DummySource(), _DummyParsed(2)
    parsed.blocks[0].text = {
        "page_body": "第一步操作。",
        "source_only": "版本修订记录。",
        "new_page": "独立主题说明。",
    }[route]
    parsed.blocks[0].chars = len(parsed.blocks[0].text)
    parsed.blocks[1].text = "第二步操作。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    calls = []
    original = _candidate()
    wire_candidate = None

    def completion(**kwargs):
        nonlocal wire_candidate
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            assert payload["plan_protocol"] == "document-plan-v5"
            wire_candidate = _wire_ranges(payload, original)
            return response(wire_candidate, tokens=12)
        request = payload["repair_request"]
        assert request["repair_protocol"] == "document-plan-repair-v3"
        assert request["candidate"] == wire_candidate
        item = next(item for item in request["items"] if item["kind"] == "coverage_gap")
        if route == "page_body":
            decision = {
                "decision_id": item["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": item["allowed_destinations"]["page_refs"],
            }
        elif route == "source_only":
            decision = {
                "decision_id": item["decision_id"],
                "decision": "source_only",
                "reason": "版本元信息，原文保留。",
            }
        else:
            decision = {
                "decision_id": item["decision_id"],
                "decision": "new_page",
                "kind": "concept",
                "title": "独立主题",
                "purpose": "说明独立主题",
                "type": None,
            }
        return response(
            {
                "repair_protocol": "document-plan-repair-v3",
                "candidate_hash": request["candidate_hash"],
                "decisions": [decision],
            },
            tokens=9,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
        assert len(calls) == 2
        assert plan.pages[0].title == "执行流程"
        assert plan.pages[0].name.startswith("concepts/page-")
        assert plan.pages[0].quality == "planned"
        assert plan.pages[0].state == "ready"
        assert plan.overview.text == original["overview"]["text"]
        assert plan.pages[0].purpose == original["page_changes"][0]["purpose"]
        if route == "new_page":
            assert len(plan.pages) == 2
        with processing_scope(SETTINGS):
            resumed = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                resume=True,
                plan_only=True,
            )
        assert resumed.pages[0].name == plan.pages[0].name
        assert len(calls) == 2


def test_v3_normal_path_needs_one_dispatch(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(1)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    value = deepcopy(_candidate())
    value["overview"]["ranges"] = [[0, 1]]
    value["page_changes"][0]["subject_ranges"] = [[0, 1]]
    count = 0

    def completion(**kwargs):
        nonlocal count
        count += 1
        payload = json.loads(kwargs["messages"][-1]["content"])
        return response(_wire_ranges(payload, value), tokens=8)

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
    assert count == 1
    assert plan.pages[0].quality == "planned"


def test_source_conflicts_route_through_real_planning_dispatch(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(3)
    for block, text in zip(parsed.blocks, ["Metadata", "Shared claim", "Page body"], strict=True):
        block.text, block.chars = text, len(text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            initial = _candidate()
            initial["overview"]["ranges"] = [[0, 3]]
            initial["page_changes"][0]["subject_ranges"] = [[1, 3]]
            initial["source_only"] = [{"ranges": [[0, 1], [1, 2]], "reason": "Mixed source"}]
            return response(_wire_ranges(payload, initial), tokens=10)
        request = payload["repair_request"]
        assert payload["response_mode"] == "routing"
        assert len(request["items"]) == 1
        item = request["items"][0]
        assert item["kind"] == "source_only_conflict"
        return response(
            {
                "repair_protocol": "document-plan-repair-v3",
                "candidate_hash": request["candidate_hash"],
                "decisions": [
                    {
                        "decision_id": item["decision_id"],
                        "decision": "route_source_only",
                        "retained_pieces": [item["source_pieces"][0]["piece_ref"]],
                        "reason": "Only the source owns this shared claim",
                    }
                ],
            },
            tokens=8,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
    assert len(calls) == 2
    assert plan.pages[0].subject_ranges == [[2, 3]]
    assert plan.source_only[0].ranges == [[0, 2]]


def test_mixed_diagnostics_and_alias_repair_in_one_normal_dispatch(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(5)
    for index, block in enumerate(parsed.blocks):
        block.text = ["metadata", "body A", "body B", "attachment reference", "tail note"][index]
        block.chars = len(block.text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    candidate = {
        "overview": {"text": "Source overview.", "ranges": [[0, 5]], "limitations": []},
        "page_changes": [
            {
                "local_key": "a",
                "kind": "concept",
                "title": "Body A",
                "purpose": "Explain body A",
                "subject_ranges": [[1, 2]],
            },
            {
                "local_key": "b",
                "kind": "concept",
                "title": "Body B",
                "purpose": "Explain body B",
                "subject_ranges": [[2, 3]],
                "necessary_context": [],
            },
        ],
        "source_only": [
            {"ranges": [[1, 2], [2, 3]], "reason": "Wrong routing"},
            {
                "ranges": [{"from_block": "unknown", "through_block": "unknown"}],
                "reason": "Bad reference",
            },
        ],
        "unresolved": [],
        "resolutions": [],
    }
    sent = []
    wire_candidate = None

    def completion(**kwargs):
        nonlocal wire_candidate
        messages = kwargs["messages"]
        sent.append(deepcopy(messages))
        payload = json.loads(messages[-1]["content"])
        if len(sent) == 1:
            assert payload["plan_protocol"] == "document-plan-v5"
            wire_candidate = _wire_ranges(payload, candidate)
            return response(wire_candidate, tokens=12)
        assert payload["plan_protocol"] == "document-plan-repair-v2"
        assert payload["response_mode"] == "patch"
        assert "Return only valid JSON using document-plan-v4" not in payload["task_rules"]
        assert "repair_request.response_contract" in payload["task_rules"]
        assert messages[:-1] == sent[0][:-1]
        assert (
            messages[-1]["content"].split(',"stage":', 1)[0]
            == sent[0][-1]["content"].split(',"stage":', 1)[0]
        )
        request = payload["repair_request"]
        assert {
            "missing_field",
            "source_only_conflict",
            "unknown_block_reference",
            "coverage_pending",
        } <= {issue["code"] for issue in request["issues"]}
        assert request["response_contract"]["operations"]["replace_field"]["value"]
        grants = request["allowed_operations"]

        def operation(code, op, field, value):
            grant = next(
                row
                for row in grants
                if row["code"] == code and row["op"] == op and row["field"] == field
            )
            return {
                "issue_id": grant["issue_id"],
                "op": op,
                "item_ref": grant["item_ref"],
                "field": field,
                "value": value,
            }

        fill_context = operation("missing_field", "replace_field", "necessary_context", [])
        fill_context["content"] = fill_context.pop("value")
        conflict_grant = next(
            row
            for row in grants
            if row["op"] == "resolve_source_only" and row["code"] == "source_only_conflict"
        )
        unknown_grant = next(
            row
            for row in grants
            if row["op"] == "resolve_source_only" and row["code"] == "unknown_block_reference"
        )
        return response(
            {
                "repair_protocol": "document-plan-repair-v2",
                "candidate_hash": request["candidate_hash"],
                "operations": [
                    fill_context,
                    {
                        "issue_id": conflict_grant["issue_id"],
                        "op": "resolve_source_only",
                        "item_ref": conflict_grant["item_ref"],
                        "value": {"decision": "discard_claim"},
                    },
                    {
                        "issue_id": unknown_grant["issue_id"],
                        "op": "resolve_source_only",
                        "item_ref": unknown_grant["item_ref"],
                        "value": {"decision": "discard_claim"},
                    },
                    operation(
                        "coverage_pending",
                        "replace_field",
                        "subject_ranges",
                        [_selection(payload, 0, 1)],
                    ),
                    operation(
                        "coverage_pending",
                        "append_item",
                        "source_only",
                        {
                            "ranges": [_selection(payload, 3)],
                            "reason": "Attachment reference retained",
                        },
                    ),
                    operation(
                        "coverage_pending",
                        "append_item",
                        "source_only",
                        {"ranges": [_selection(payload, 4)], "reason": "Tail note retained"},
                    ),
                ],
            },
            tokens=16,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
    assert len(sent) == 2
    assert len(plan.pages) == 2
    assert len(plan.source_only) == 2
    assert not list((workspace / "wiki").rglob("*.md"))


def test_pending_v3_resume_requests_only_remaining_patch(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(2)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            return response(_wire_ranges(payload, _candidate()), tokens=8)
        request = payload["repair_request"]
        item = request["items"][0]
        return response(
            {
                "repair_protocol": "document-plan-repair-v3",
                "candidate_hash": request["candidate_hash"],
                "decisions": [
                    {
                        "decision_id": item["decision_id"],
                        "decision": "source_only",
                        "reason": "Metadata only",
                    }
                ],
            },
            tokens=8,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    original_invalid = V3PlanningRepairSession.invalid
    interrupted = False

    def interrupt_after_draft(self, raw, result, attempt):
        nonlocal interrupted
        feedback = original_invalid(self, raw, result, attempt)
        if not interrupted:
            interrupted = True
            raise ProcessingIncomplete("user_stop", "planning")
        return feedback

    monkeypatch.setattr(V3PlanningRepairSession, "invalid", interrupt_after_draft)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS), pytest.raises(ProcessingIncomplete, match="user_stop"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
        assert len(calls) == 1
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                resume=True,
                plan_only=True,
            )
    assert len(calls) == 2
    assert plan.pages[0].quality == "planned"


def test_v4_two_windows_accumulate_overview_and_page_without_old_block_ids(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(2)
    for index, block in enumerate(parsed.blocks):
        block.text = f"Step {index}."
        block.chars = len(block.text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    requests = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        requests.append(payload)
        block_id = payload["evidence"]["blocks"][0]["id"]
        selected = {"from_block": block_id, "through_block": block_id}
        if len(requests) == 1:
            assert payload["carry"]["page_register"] == []
            page = {
                "local_key": "first",
                "kind": "concept",
                "title": "Two steps",
                "purpose": "Explain both steps",
                "subject_ranges": [selected],
                "necessary_context": [],
            }
        else:
            assert len(payload["carry"]["page_register"]) == 1
            assert payload["carry"]["overview"] == "First step."
            page = {
                "local_key": "second",
                "target_key": payload["carry"]["page_register"][0]["key"],
                "kind": "concept",
                "title": "Two steps",
                "purpose": "Explain both steps",
                "subject_ranges": [selected],
                "necessary_context": [],
            }
        return response(
            {
                "overview": {
                    "text": "First step." if len(requests) == 1 else "First and second steps.",
                    "ranges": [selected],
                    "limitations": [],
                },
                "page_changes": [page],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            },
            tokens=8,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    navigation = _two_window_navigation(source, parsed, "nav-v4-two-windows")
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
    assert len(requests) == 2
    assert plan.overview.text == "First and second steps."
    assert plan.overview.ranges == [[0, 1], [1, 2]]
    assert len(plan.pages) == 1
    assert plan.pages[0].subject_ranges == [[0, 1], [1, 2]]
