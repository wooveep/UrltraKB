"""Small fixed responses exercise the planning reference gate and its wire contract."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_page_evidence import page_evidence
from openkb.agent.document_plan_compiler import PlanningContext
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_planning_support import fallback_read_evidence
from openkb.agent.document_reference_check import (
    PROTOCOL,
    ReferenceCandidate,
    apply_reference_decisions,
    detect_references,
    target_pairs,
    validate_reference_decisions,
)
from openkb.agent.document_reference_evidence import (
    ReferenceEvidenceError,
    read_reference_evidence,
)
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import DEFAULT_PROCESSING, ProcessingIncomplete, processing_scope
from openkb.sources import content_id
from tests.test_adaptive_processing import response
from tests.test_document_orchestrator import _DummyParsed, _DummySource

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


def _navigation(source, parsed):
    return {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": [
            {"id": "n0", "parent": None, "start": 0, "end": 4, "title": "流程"},
            {"id": "n1", "parent": "n0", "start": 0, "end": 2, "title": "同步前准备"},
            {"id": "n2", "parent": "n0", "start": 2, "end": 4, "title": "批量同步"},
        ],
    }


def _plan(payload, *, external=False, informational=False):
    ids = [row["id"] for row in payload["evidence"]["blocks"]]

    def span(first, last):
        return [{"from_block": ids[first], "through_block": ids[last]}]

    return {
        "overview": {"text": "同步前准备和批量同步。", "ranges": span(0, 3), "limitations": []},
        "page_changes": [
            {
                "local_key": "prep",
                "kind": "concept",
                "title": "准备",
                "purpose": "准备同步",
                "subject_ranges": span(0, 1),
                "necessary_context": [],
            },
            {
                "local_key": "sync",
                "kind": "concept",
                "title": "同步",
                "purpose": "批量同步",
                "subject_ranges": span(2, 3),
                "necessary_context": [],
            },
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }


@pytest.mark.parametrize(
    "decision",
    [
        "required_internal",
        "targeted_required_internal",
        "required_unavailable",
        "informational",
        "uncertain",
        "malformed_then_required_internal",
        "empty_then_required_internal",
        "routing_then_required_internal",
    ],
)
def test_reference_gate_decisions_through_real_dispatch(tmp_path, monkeypatch, decision):
    source, parsed = _DummySource(), _DummyParsed(4)
    target = "《凭据操作手册》" if decision == "required_unavailable" else "“同步前准备”"
    instruction = "扩展阅读仅供参考：参见" if decision == "informational" else "先执行"
    texts = [
        "同步前准备",
        "核对源端校验码，并记录同步起点。",
        "批量同步",
        f"{instruction}{target}，再提交同步批次。",
    ]
    for block, text in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = text, len(text)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    if decision == "targeted_required_internal":
        attempts = []

        def fits(_limits, _model, messages):
            attempts.append(json.loads(messages[-1]["content"])["prefix_mode"])
            return len(attempts) > 1

        monkeypatch.setattr("openkb.agent.document_reference_review._fits", fits)
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
        if payload["response_mode"] == "plan":
            candidate = _plan(payload)
            if decision == "routing_then_required_internal":
                candidate["page_changes"][1]["subject_ranges"] = [
                    {
                        "from_block": payload["evidence"]["blocks"][3]["id"],
                        "through_block": payload["evidence"]["blocks"][3]["id"],
                    }
                ]
            return response(candidate, tokens=10)
        if payload["response_mode"] == "routing":
            request = payload["repair_request"]
            assert decision == "routing_then_required_internal"
            return response(
                {
                    "repair_protocol": "document-plan-repair-v3",
                    "candidate_hash": request["candidate_hash"],
                    "decisions": [
                        {
                            "decision_id": item["decision_id"],
                            "decision": "attach_to_pages",
                            "page_refs": [
                                request["items"][0]["allowed_destinations"]["page_refs"][1]
                            ],
                        }
                        for item in request["items"]
                    ],
                },
                tokens=8,
            )
        assert payload["response_mode"] == "reference_check"
        if decision == "targeted_required_internal":
            assert payload["prefix_mode"] == "targeted"
            assert payload["supplemental_evidence"] is None
        if decision == "malformed_then_required_internal" and len(calls) == 2:
            broken = response({}, tokens=8)
            broken.choices[0].message.content = '{"check_protocol":'
            return broken
        if decision == "empty_then_required_internal" and len(calls) == 2:
            blank = response({}, tokens=8)
            blank.choices[0].message.content = None
            return blank
        assert payload["requested_pairs"][0]["page_ref"] == "sync"
        assert len(payload["requested_pairs"]) == 1
        basis = payload["references"][0]["basis_ranges"]
        effective = (
            "required_internal"
            if decision
            in {
                "malformed_then_required_internal",
                "empty_then_required_internal",
                "targeted_required_internal",
                "routing_then_required_internal",
            }
            else decision
        )
        item = {
            **payload["requested_pairs"][0],
            "decision": effective,
            "reason": "该引用按原文判断。",
        }
        if effective == "required_internal":
            item["target_ranges"] = [
                {
                    "from_block": payload["evidence"]["blocks"][1]["id"],
                    "through_block": payload["evidence"]["blocks"][1]["id"],
                }
            ]
        elif effective in {"informational", "uncertain"}:
            item["decision_basis_ranges"] = basis
        return response(
            {
                "check_protocol": "document-reference-check-v3",
                "decisions": [item],
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
                _navigation(source, parsed),
                SETTINGS,
                checkpoints,
                plan_only=True,
            )
        assert len(calls) == (
            1
            if decision == "required_unavailable"
            else 3
            if decision
            in {
                "malformed_then_required_internal",
                "empty_then_required_internal",
                "routing_then_required_internal",
            }
            else 2
        )
        sync = next(page for page in plan.pages if page.title == "同步")
        if decision in {
            "required_internal",
            "malformed_then_required_internal",
            "empty_then_required_internal",
            "targeted_required_internal",
            "routing_then_required_internal",
        }:
            assert sync.necessary_context[0]["basis_quote"] in texts[3]
            assert sync.necessary_context[0]["ranges"] == [[1, 2]]
            assert sync.state == "ready"
        elif decision == "informational":
            assert not sync.necessary_context and not plan.unresolved
            assert sync.state == "ready"
        elif decision == "required_unavailable":
            assert sync.state == "ready"
            assert sync.limitations
            assert plan.external_references
            assert not plan.unresolved
        else:
            assert sync.state == "blocked"
            assert any(sync.key in issue.affected_pages for issue in plan.unresolved)
        assert (
            plan.metadata["accepted_window_receipts"][0]["reference_check"]["status"] == "accounted"
        )
        check_receipt = plan.metadata["accepted_window_receipts"][0]["reference_check"]
        if "receipt_key" in check_receipt:
            assert (
                checkpoints.load_recovery(check_receipt["receipt_key"], "reference_check")[
                    "receipt"
                ]["status"]
                == "accounted"
            )
        preview = Path(plan.metadata["plan_preview"])
        assert preview.is_file()
        assert "## 概要" in preview.read_text(encoding="utf-8")
        assert "## 引用核对" in preview.read_text(encoding="utf-8")
        with processing_scope(SETTINGS):
            resumed = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                _navigation(source, parsed),
                SETTINGS,
                checkpoints,
                resume=True,
                plan_only=True,
            )
        assert len(calls) == (
            1
            if decision == "required_unavailable"
            else 3
            if decision
            in {
                "malformed_then_required_internal",
                "empty_then_required_internal",
                "routing_then_required_internal",
            }
            else 2
        )
        if decision == "empty_then_required_internal":
            assert calls[1] == calls[2]
        assert resumed.pages[0].name == plan.pages[0].name


def test_two_empty_reference_answers_stop_without_accepting_window(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(4)
    texts = ["同步前准备", "核对源端校验码。", "批量同步", "先执行“同步前准备”，再同步。"]
    for block, body in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = body, len(body)
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
        if payload["response_mode"] == "plan":
            return response(_plan(payload), tokens=8)
        answer = response({}, tokens=8)
        answer.choices[0].message.content = None
        return answer

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            assert (
                plan_document(
                    tmp_path,
                    workspace,
                    source,
                    parsed,
                    _navigation(source, parsed),
                    SETTINGS,
                    checkpoints,
                    plan_only=True,
                )
                is None
            )
        with processing_scope(SETTINGS):
            assert (
                plan_document(
                    tmp_path,
                    workspace,
                    source,
                    parsed,
                    _navigation(source, parsed),
                    SETTINGS,
                    checkpoints,
                    resume=True,
                    plan_only=True,
                )
                is None
            )
    assert len(calls) == 3
    assert calls[1] == calls[2]
    assert not list(workspace.glob("**/*plan-preview*"))


def _validator():
    blocks = [
        SimpleNamespace(id=f"{index + 1:064x}", chars=8, text="abcdefgh") for index in range(3)
    ]
    parsed = SimpleNamespace(blocks=blocks)
    evidence = {
        "blocks": [
            {"id": block.id, "order": index, "text": block.text}
            for index, block in enumerate(blocks)
        ]
    }
    context = PlanningContext("s", "p", "t", evidence, parsed, 0, 3, 3)
    candidates = [
        ReferenceCandidate(
            "r1",
            ({"block": blocks[0].id, "start_char": 0, "end_char": 4},),
            "target",
            ({"start": 1, "end": 2},),
            "in_window",
            ("p1", "p2"),
        )
    ]
    return SelectionResolver.from_context(context), candidates, blocks


def test_decision_normalization_and_pair_order_are_stable():
    resolver, candidates, blocks = _validator()
    decisions = [
        {
            "reference_key": "r1",
            "page_ref": page,
            "decision": "required_internal",
            "reason": "required",
            "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
        }
        for page in ("p2", "p1")
    ]
    raw = {
        "check_protocol": "document-reference-check-v1",
        "candidate_hash": "c",
        "check_input_hash": "i",
        "decisions": [*decisions, deepcopy(decisions[0])],
    }
    result = validate_reference_decisions(
        "\ufeff```json\n" + json.dumps(raw) + "\n```",
        candidate_hash="c",
        check_input_hash="i",
        pairs=[("r1", "p1"), ("r1", "p2")],
        resolver=resolver,
        candidates=candidates,
    )
    assert not result.issues
    assert set(result.valid) == {("r1", "p1"), ("r1", "p2")}
    assert "duplicate_identical_decision" in result.normalized


def test_informational_basis_must_cover_the_actual_reference_words():
    resolver, candidates, blocks = _validator()
    raw = {
        "check_protocol": "document-reference-check-v1",
        "candidate_hash": "c",
        "check_input_hash": "i",
        "decisions": [
            {
                "reference_key": "r1",
                "page_ref": "p1",
                "decision": "informational",
                "reason": "The source labels this optional.",
                "decision_basis_ranges": [{"block": blocks[0].id, "start_char": 4, "end_char": 8}],
            }
        ],
    }
    result = validate_reference_decisions(
        raw,
        candidate_hash="c",
        check_input_hash="i",
        pairs=[("r1", "p1")],
        resolver=resolver,
        candidates=candidates,
    )
    assert ("r1", "p1") not in result.valid
    assert any(issue.code == "reference_target_outside_evidence" for issue in result.issues)


def test_existing_complete_context_and_precise_unresolved_are_not_duplicated():
    resolver, candidates, blocks = _validator()
    whole_basis = [{"from_block": blocks[0].id, "through_block": blocks[0].id}]
    whole_target = [{"from_block": blocks[1].id, "through_block": blocks[1].id}]
    plan = {
        "page_changes": [
            {
                "local_key": "p1",
                "necessary_context": [
                    {
                        "relation": "explicit_reference",
                        "ranges": whole_target,
                        "basis_ranges": whole_basis,
                    }
                ],
            }
        ],
        "unresolved": [
            {
                "location": whole_basis,
                "missing_target": "《外部审批手册》的办理流程",
                "affected_pages": ["p1"],
            }
        ],
    }
    internal = {
        "decision": "required_internal",
        "reason": "required",
        "target_ranges": [{"block": blocks[1].id, "start_char": 0, "end_char": 8}],
    }
    external = ReferenceCandidate(
        "r2",
        ({"block": blocks[0].id, "start_char": 0, "end_char": 4},),
        "外部审批手册",
        (),
        "external_not_supplied",
        ("p1",),
    )
    updated = apply_reference_decisions(
        plan,
        {
            ("r1", "p1"): internal,
            ("r2", "p1"): {"decision": "required_unavailable", "reason": "missing"},
        },
        [*candidates, external],
        resolver=resolver,
    )
    assert len(updated["page_changes"][0]["necessary_context"]) == 1
    assert len(updated["unresolved"]) == 1


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda raw: raw.update(decisions=None), "reference_check_invalid"),
        (lambda raw: raw["decisions"].clear(), "reference_decision_missing"),
        (
            lambda raw: raw["decisions"][0].update(decision="not_required"),
            "reference_check_invalid",
        ),
        (lambda raw: raw.update(check_input_hash="old"), "reference_input_mismatch"),
        (
            lambda raw: raw["decisions"][0]["target_ranges"][0].update(from_block="unknown"),
            "reference_target_outside_evidence",
        ),
    ],
)
def test_invalid_decisions_do_not_change_candidate(mutation, code):
    resolver, candidates, blocks = _validator()
    raw = {
        "check_protocol": "document-reference-check-v1",
        "candidate_hash": "c",
        "check_input_hash": "i",
        "decisions": [
            {
                "reference_key": "r1",
                "page_ref": "p1",
                "decision": "required_internal",
                "reason": "required",
                "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
            }
        ],
    }
    before = deepcopy(raw)
    mutation(raw)
    result = validate_reference_decisions(
        raw,
        candidate_hash="c",
        check_input_hash="i",
        pairs=[("r1", "p1")],
        resolver=resolver,
        candidates=candidates,
    )
    assert any(issue.code == code for issue in result.issues)
    assert before["decisions"][0]["target_ranges"][0]["from_block"] == blocks[1].id


def test_v2_response_uses_request_bound_identity_without_model_hash_echo():
    resolver, candidates, blocks = _validator()
    decision = {
        "reference_key": "r1",
        "page_ref": "p1",
        "decision": "required_internal",
        "reason": "The referenced preparation is required",
        "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
    }
    raw = {"check_protocol": PROTOCOL, "decisions": [decision]}
    kwargs = {
        "candidate_hash": "candidate",
        "check_input_hash": "request",
        "pairs": [("r1", "p1")],
        "resolver": resolver,
        "candidates": candidates,
        "required_protocol": PROTOCOL,
    }
    accepted = validate_reference_decisions(raw, **kwargs)
    assert not accepted.issues
    assert ("r1", "p1") in accepted.valid
    with_echo = {**raw, "check_input_hash": "wrong"}
    assert (
        validate_reference_decisions(with_echo, **kwargs).issues[0].code
        == "reference_check_invalid"
    )
    old = {
        **raw,
        "check_protocol": "document-reference-check-v1",
        "candidate_hash": "candidate",
        "check_input_hash": "request",
    }
    assert validate_reference_decisions(old, **kwargs).issues[0].code == "reference_input_mismatch"


def test_replayed_accepted_reference_decision_is_idempotent_but_changed_echo_conflicts():
    resolver, candidates, blocks = _validator()
    accepted = {
        "reference_key": "r1",
        "page_ref": "p1",
        "decision": "required_internal",
        "reason": "The referenced preparation is required",
        "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
    }
    kwargs = {
        "candidate_hash": "candidate",
        "check_input_hash": "request",
        "pairs": [],
        "accepted_decisions": {("r1", "p1"): accepted},
        "resolver": resolver,
        "candidates": candidates,
        "required_protocol": PROTOCOL,
    }
    replay = validate_reference_decisions(
        {"check_protocol": PROTOCOL, "decisions": [deepcopy(accepted)]}, **kwargs
    )
    assert replay.issues == ()
    assert replay.valid == {}
    assert "replayed_accepted_decision" in replay.normalized

    changed = {**accepted, "reason": "A different reason"}
    conflict = validate_reference_decisions(
        {"check_protocol": PROTOCOL, "decisions": [changed]}, **kwargs
    )
    assert conflict.valid == {}
    assert any(issue.code == "reference_decision_conflict" for issue in conflict.issues)


def test_duplicate_json_field_is_not_silently_overwritten():
    resolver, candidates, _ = _validator()
    raw = (
        '{"check_protocol":"document-reference-check-v1",'
        '"candidate_hash":"c","candidate_hash":"c",'
        '"check_input_hash":"i","decisions":[]}'
    )
    result = validate_reference_decisions(
        raw,
        candidate_hash="c",
        check_input_hash="i",
        pairs=[("r1", "p1")],
        resolver=resolver,
        candidates=candidates,
    )
    assert result.issues[0].code == "reference_check_invalid"


def test_conflicting_pair_cannot_leave_first_decision_applicable():
    resolver, candidates, blocks = _validator()
    first = {
        "reference_key": "r1",
        "page_ref": "p1",
        "decision": "required_internal",
        "reason": "required",
        "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
    }
    second = {
        "reference_key": "r1",
        "page_ref": "p1",
        "decision": "informational",
        "reason": "optional",
        "decision_basis_ranges": [{"block": blocks[0].id, "start_char": 0, "end_char": 4}],
    }
    raw = {
        "check_protocol": "document-reference-check-v1",
        "candidate_hash": "c",
        "check_input_hash": "i",
        "decisions": [first, second],
    }
    result = validate_reference_decisions(
        raw,
        candidate_hash="c",
        check_input_hash="i",
        pairs=[("r1", "p1")],
        resolver=resolver,
        candidates=candidates,
    )
    assert ("r1", "p1") not in result.valid
    assert any(issue.code == "reference_decision_conflict" for issue in result.issues)


def test_located_target_read_failure_is_technical_pending(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(2)
    candidate = ReferenceCandidate(
        "r1",
        ({"block": parsed.blocks[0].id, "start_char": 0, "end_char": 4},),
        "preparation",
        ({"start": 1, "end": 2},),
        "outside_window",
        ("p1",),
    )
    evidence = fallback_read_evidence(source, parsed, 0, 1)

    class BrokenReader:
        def complete_bound(self, _reference):
            return 100

        def read(self, _reference, *, max_chars):
            raise OSError("source unavailable")

    with pytest.raises(ReferenceEvidenceError) as error:
        read_reference_evidence(
            tmp_path, source, parsed, evidence, [candidate], reader=BrokenReader()
        )
    assert error.value.code == "reference_evidence_read_failed"


def test_complete_target_in_frozen_window_does_not_trigger_duplicate_read(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(2)
    candidate = ReferenceCandidate(
        "r1",
        ({"block": parsed.blocks[0].id, "start_char": 0, "end_char": 4},),
        "preparation",
        ({"start": 1, "end": 2},),
        "in_window",
        ("p1",),
    )
    evidence = fallback_read_evidence(source, parsed, 0, 2)
    target = evidence["blocks"][1]
    target["reference"] = {
        "block_id": target["id"],
        "start": 0,
        "end": parsed.blocks[1].chars,
    }

    class UnexpectedReader:
        def read(self, _reference, *, max_chars):
            pytest.fail("complete W target was read again")

    extra = read_reference_evidence(
        tmp_path, source, parsed, evidence, [candidate], reader=UnexpectedReader()
    )
    assert extra.blocks == ()
    assert extra.receipts == ()


def test_same_title_under_two_parents_remains_ambiguous():
    blocks = [
        SimpleNamespace(id=f"{index + 1:064x}", chars=len(body), text=body)
        for index, body in enumerate(["场景 A", "准备", "场景 B", "准备", "先执行“准备”，再提交。"])
    ]
    parsed = SimpleNamespace(blocks=blocks)
    evidence = {
        "parse_id": "parse",
        "blocks": [
            {"id": block.id, "order": index, "text": block.text}
            for index, block in enumerate(blocks)
        ],
    }
    nav = {
        "nodes": [
            {"id": "a", "title": "准备", "parent": "A", "start": 1, "end": 2},
            {"id": "b", "title": "准备", "parent": "B", "start": 3, "end": 4},
        ]
    }
    delta = {"page_changes": [{"local_key": "sync", "subject_ranges": [[4, 5]]}], "unresolved": []}
    found = detect_references(evidence, nav, delta, parsed)
    assert len(found) == 1
    assert found[0].availability == "ambiguous"
    assert {node["id"] for node in found[0].target_options} == {"a", "b"}


def test_reference_updates_are_atomic_and_idempotent():
    resolver, candidates, blocks = _validator()
    candidate = {
        "page_changes": [
            {"local_key": "p1", "necessary_context": []},
            {"local_key": "p2", "necessary_context": []},
        ],
        "unresolved": [],
    }
    decision = {
        "reference_key": "r1",
        "page_ref": "p1",
        "decision": "required_internal",
        "reason": "required",
        "target_ranges": [{"from_block": blocks[1].id, "through_block": blocks[1].id}],
    }
    changed = apply_reference_decisions(candidate, {("r1", "p1"): decision}, candidates)
    assert not candidate["page_changes"][0]["necessary_context"]
    assert (
        len(
            apply_reference_decisions(changed, {("r1", "p1"): decision}, candidates)[
                "page_changes"
            ][0]["necessary_context"]
        )
        == 1
    )
    assert content_id(changed["page_changes"][1]) == content_id(candidate["page_changes"][1])


def test_partial_target_fragment_cannot_count_as_complete_page_context():
    resolver, candidates, _ = _validator()
    block = SimpleNamespace(id=candidates[0].basis_ranges[0]["block"], chars=8)
    target = SimpleNamespace(id=resolver.by_order[1]["id"], chars=8)
    source = SimpleNamespace(blocks=[block, target])
    candidate = ReferenceCandidate(
        "r1", candidates[0].basis_ranges, "target", ({"start": 1, "end": 2},), "in_window", ("p1",)
    )
    delta = {
        "page_changes": [
            {
                "local_key": "p1",
                "subject_ranges": [[0, 1]],
                "necessary_context": [
                    {"ranges": [{"block_index": 1, "start_char": 0, "end_char": 3}]}
                ],
            }
        ],
        "unresolved": [],
    }
    assert target_pairs([candidate], delta, source) == [("r1", "p1")]


@pytest.mark.parametrize("route", ["same_page", "context", "overlapping_subject"])
def test_complete_target_on_operation_page_is_already_accounted_for(route):
    blocks = [SimpleNamespace(id=f"{index + 1:064x}", chars=8) for index in range(4)]
    parsed = SimpleNamespace(blocks=blocks)
    candidate = ReferenceCandidate(
        "r1",
        ({"block": blocks[3].id, "start_char": 0, "end_char": 4},),
        "preparation",
        ({"start": 0, "end": 2},),
        "in_window",
        ("sync",),
    )
    if route == "same_page":
        subject, contexts = [[0, 4]], []
    elif route == "context":
        subject, contexts = [[2, 4]], [{"ranges": [[0, 2]]}]
    else:
        subject, contexts = [[0, 2], [2, 4]], []
    delta = {
        "page_changes": [
            {"local_key": "sync", "subject_ranges": subject, "necessary_context": contexts}
        ],
        "unresolved": [],
    }
    assert target_pairs([candidate], delta, parsed) == []


def test_future_window_target_is_read_as_context_without_advancing_its_body(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(4)
    texts = ["批量同步", "先执行“同步前准备”，再提交同步批次。", "同步前准备", "核对源端校验码。"]
    for block, body in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = body, len(body)
    windows = [
        {
            "evidence": evidence_descriptor(source, parsed, left, right),
            "target_start": left,
            "target_end": right,
            "status": "complete",
            "reason": "",
            "target_tokens": 1000,
        }
        for left, right in ((0, 2), (2, 4))
    ]
    navigation = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "windows": windows,
        "nodes": [
            {"id": "n1", "parent": None, "title": "批量同步", "start": 0, "end": 2},
            {"id": "n2", "parent": None, "title": "同步前准备", "start": 2, "end": 4},
        ],
    }
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )

    class Reader:
        def complete_bound(self, reference):
            return parsed.blocks[int(reference.block_id, 16)].chars

        def read(self, reference, *, max_chars):
            body = parsed.blocks[int(reference.block_id, 16)].text
            return SimpleNamespace(text=body[reference.start : reference.end], location={})

    monkeypatch.setattr(
        "openkb.agent.document_reference_evidence.ParseStore",
        lambda _kb: SimpleNamespace(reader=lambda _source, _parsed: Reader()),
    )
    messages = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        messages.append(payload)
        if payload["response_mode"] == "reference_check":
            assert payload["supplemental_evidence"]["blocks"][1]["text"] == texts[3]
            pair = payload["requested_pairs"][0]
            target_id = payload["supplemental_evidence"]["blocks"][1]["id"]
            return response(
                {
                    "check_protocol": "document-reference-check-v3",
                    "decisions": [
                        {
                            **pair,
                            "decision": "required_internal",
                            "reason": "先完成准备",
                            "target_ranges": [
                                {"from_block": target_id, "through_block": target_id}
                            ],
                        }
                    ],
                },
                tokens=8,
            )
        start = payload["target"]["target_start"]
        ids = [row["id"] for row in payload["evidence"]["blocks"]]
        selected = [{"from_block": ids[0], "through_block": ids[1]}]
        return response(
            {
                "overview": {"text": "同步流程。", "ranges": selected, "limitations": []},
                "page_changes": [
                    {
                        "local_key": "sync" if start == 0 else "prep",
                        "kind": "concept",
                        "title": "同步" if start == 0 else "准备",
                        "purpose": "流程",
                        "subject_ranges": selected,
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            },
            tokens=10,
        )

    monkeypatch.setattr(litellm, "completion", completion)
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
    assert [row["response_mode"] for row in messages] == ["plan", "reference_check", "plan"]
    sync = next(page for page in plan.pages if page.title == "同步")
    assert sync.subject_ranges == [[0, 2]]
    assert sync.necessary_context[0]["ranges"] == [[3, 4]]
    _, occurrences = page_evidence(sync, Reader(), source, parsed)
    assert any(row["text"] == texts[3] for row in occurrences)
    assert next(page for page in plan.pages if page.title == "准备").subject_ranges == [[2, 4]]
    assert plan.metadata["accepted_window_receipts"][0]["target_ranges"] == [[0, 2]]


@pytest.mark.parametrize("empty_mid", [False, True])
def test_mixed_decisions_repair_only_invalid_pair_before_atomic_commit(
    tmp_path, monkeypatch, empty_mid
):
    source, parsed = _DummySource(), _DummyParsed(4)
    texts = [
        "同步前准备",
        "核对源端校验码。",
        "批量同步",
        "先执行“同步前准备”，再参见《背景说明》了解实现原理。",
    ]
    for block, body in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = body, len(body)
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
        if payload["response_mode"] == "plan":
            return response(_plan(payload), tokens=10)
        refs = {row["reference_key"]: row for row in payload["references"]}
        pairs = payload["requested_pairs"]
        assert len(refs) == 2
        if len(calls) == 2:
            assert len(pairs) == 2
            answers = []
            for pair in pairs:
                row = refs[pair["reference_key"]]
                if "背景说明" in row["target_text"]:
                    answers.append(
                        {
                            **pair,
                            "decision": "informational",
                            "reason": "仅供了解",
                            "decision_basis_ranges": row["basis_ranges"],
                        }
                    )
                else:
                    answers.append(
                        {
                            **pair,
                            "decision": "required_internal",
                            "reason": "先准备",
                            "target_ranges": [
                                {"from_block": "unknown", "through_block": "unknown"}
                            ],
                        }
                    )
        elif empty_mid and len(calls) == 3:
            assert len(pairs) == 1
            assert len(payload["accepted_decisions"]) == 1
            blank = response({}, tokens=8)
            blank.choices[0].message.content = None
            return blank
        else:
            assert len(pairs) == 1
            assert len(payload["accepted_decisions"]) == 1
            target_id = payload["evidence"]["blocks"][1]["id"]
            answers = [
                {
                    **pairs[0],
                    "decision": "required_internal",
                    "reason": "先准备",
                    "target_ranges": [{"from_block": target_id, "through_block": target_id}],
                }
            ]
        return response(
            {
                "check_protocol": "document-reference-check-v3",
                "decisions": answers,
            },
            tokens=8,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    settings = {**SETTINGS, "processing": {**SETTINGS["processing"], "max_attempts": 3}}
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                _navigation(source, parsed),
                settings,
                checkpoints,
                plan_only=True,
            )
    assert len(calls) == (4 if empty_mid else 3)
    if empty_mid:
        assert calls[2] == calls[3]
    sync = next(page for page in plan.pages if page.title == "同步")
    assert len(sync.necessary_context) == 1
    assert not plan.unresolved


def test_two_reference_batches_share_baseline_and_commit_once(tmp_path, monkeypatch):
    from openkb.agent.document_planning_ledger import DocumentPlanningLedger

    source, parsed = _DummySource(), _DummyParsed(4)
    texts = ["同步前准备", "核对校验码。", "批量同步", "先执行“同步前准备”，参见《背景说明》。"]
    for block, body in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = body, len(body)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    settings = deepcopy(SETTINGS)
    settings["processing"]["output_tokens"] = 300
    settings["processing"]["max_output_tokens"] = 300
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    batches = []
    accepted = []
    original = DocumentPlanningLedger.apply_accepted

    def apply_once(self, *args, **kwargs):
        accepted.append(args[1]["reference_check"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentPlanningLedger, "apply_accepted", apply_once)

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["response_mode"] == "plan":
            return response(_plan(payload), tokens=10)
        batches.append(payload)
        assert len(payload["requested_pairs"]) == 1
        pair = payload["requested_pairs"][0]
        row = next(
            row for row in payload["references"] if row["reference_key"] == pair["reference_key"]
        )
        if "背景说明" in row["target_text"]:
            item = {
                **pair,
                "decision": "informational",
                "reason": "背景资料",
                "decision_basis_ranges": row["basis_ranges"],
            }
        else:
            identity = payload["evidence"]["blocks"][1]["id"]
            item = {
                **pair,
                "decision": "required_internal",
                "reason": "操作前提",
                "target_ranges": [{"from_block": identity, "through_block": identity}],
            }
        return response(
            {
                "check_protocol": "document-reference-check-v3",
                "decisions": [item],
            },
            tokens=8,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                _navigation(source, parsed),
                settings,
                checkpoints,
                plan_only=True,
            )
    assert len(batches) == 2
    assert batches[0]["requested_pairs"] != batches[1]["requested_pairs"]
    assert all("candidate_hash" not in batch for batch in batches)
    assert all("check_input_hash" not in batch for batch in batches)
    assert len(accepted) == 1
    assert accepted[0]["status"] == "accounted"
    assert len(next(page for page in plan.pages if page.title == "同步").necessary_context) == 1


def test_execution_unknown_does_not_resend_reference_check(tmp_path, monkeypatch):
    import openkb.agent.compiler as compiler

    source, parsed = _DummySource(), _DummyParsed(4)
    texts = ["同步前准备", "核对校验码。", "批量同步", "先执行“同步前准备”，再同步。"]
    for block, body in zip(parsed.blocks, texts, strict=True):
        block.text, block.chars = body, len(body)
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    dispatched = []
    original = compiler._llm_call

    def caller(model, messages, stage, **kwargs):
        payload = json.loads(messages[-1]["content"])
        dispatched.append(payload["response_mode"])
        if payload["response_mode"] == "reference_check":
            raise ProcessingIncomplete("request_execution_unknown", "planning")
        return original(model, messages, stage, **kwargs)

    monkeypatch.setattr(compiler, "_llm_call", caller)
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: response(_plan(json.loads(kwargs["messages"][-1]["content"])), tokens=8),
    )
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with processing_scope(SETTINGS):
            with pytest.raises(ProcessingIncomplete, match="request_execution_unknown"):
                plan_document(
                    tmp_path,
                    workspace,
                    source,
                    parsed,
                    _navigation(source, parsed),
                    SETTINGS,
                    checkpoints,
                    plan_only=True,
                )
        with processing_scope(SETTINGS):
            with pytest.raises(ProcessingIncomplete, match="reference_check_execution_unknown"):
                plan_document(
                    tmp_path,
                    workspace,
                    source,
                    parsed,
                    _navigation(source, parsed),
                    SETTINGS,
                    checkpoints,
                    resume=True,
                    plan_only=True,
                )
    assert dispatched.count("reference_check") == 1
