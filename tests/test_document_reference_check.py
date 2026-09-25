"""Small fixed responses exercise the planning reference gate and its wire contract."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openkb.agent.document_plan_compiler import PlanningContext
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_planning_support import fallback_read_evidence
from openkb.agent.document_reference_check import (
    PROTOCOL,
    ReferenceCandidate,
    apply_reference_decisions,
    detect_references,
    target_pairs,
    valid_saved_decisions,
    validate_reference_decisions,
)
from openkb.agent.document_reference_evidence import (
    ReferenceEvidenceError,
    read_reference_evidence,
)
from openkb.processing import DEFAULT_PROCESSING
from openkb.sources import content_id
from tests.test_document_orchestrator import _DummyParsed, _DummySource
from tests.test_document_plan_compiler import _context

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


@pytest.mark.parametrize("saved", [
    {"attempt": "x", "valid": []},
    {"attempt": 0, "status": [], "valid": []},
    {"attempt": 0, "valid": None},
    {"attempt": 0, "valid": [{"reference_key": "r"}]},
    {"attempt": 0, "status": "validated", "valid": [], "candidate": {}},
    {"attempt": 0, "status": "response_received", "valid": [],
     "response_representation": "wire", "response": None},
    {"attempt": 0, "status": "response_received", "valid": [],
     "response_representation": "wire", "response": []},
    {"attempt": 0, "status": "response_received", "valid": [],
     "response_representation": "wire", "response": "{}", "response_output_tokens": "x"},
])
def test_recovered_reference_state_rejects_malformed_rows(saved):
    context = _context(["See manual"])
    resolver = SelectionResolver.from_context(context)
    with pytest.raises(ValueError):
        valid_saved_decisions(
            saved, pairs=[], resolver=resolver, candidates=[],
            candidate_hash="candidate", check_input_hash="input",
            recovery_key="recovery", max_attempts=2,
        )


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


def test_checked_references_clear_only_matching_cross_reference_blockers():
    from tests.test_document_plan_compiler import _context

    context = replace(
        _context(["凭据准备", "先执行“凭据准备”。", "遵循《外部手册》。"]),
        selection_protocol="document-plan-v5",
    )
    ids = [block.id for block in context.parsed.blocks]

    def select(index):
        return {"from_block": ids[index], "through_block": ids[index]}
    candidate = {
        "page_changes": [{"local_key": "sync", "target_key": "p:sync",
                          "necessary_context": [], "limitations": []}],
        "unresolved": [
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "凭据准备", "location": [
                 {"block": ids[1], "start_char": 3, "end_char": 9}],
             "affected_pages": ["p:sync"]},
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "《外部手册》", "location": [
                 {"block": ids[2], "start_char": 2, "end_char": 8}],
             "affected_pages": ["sync"]},
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "另一项缺失条件", "location": [select(1)],
             "affected_pages": ["sync"]},
        ],
        "external_references": [],
    }
    internal = ReferenceCandidate(
        "internal", ({"block_index": 1, "start_char": 3, "end_char": 9},),
        "凭据准备", (), "supplied", ("sync",),
    )
    external = ReferenceCandidate(
        "external", ({"block_index": 2, "start_char": 2, "end_char": 8},),
        "《外部手册》", (), "external_not_supplied", ("sync",),
    )
    decisions = {
        ("internal", "sync"): {
            "decision": "required_internal", "reason": "先准备",
            "target_ranges": [select(0)],
        },
        ("external", "sync"): {
            "decision": "required_unavailable", "reason": "仍须遵循",
        },
    }

    updated = apply_reference_decisions(
        candidate, decisions, [internal, external], resolver=SelectionResolver.from_context(context)
    )

    assert [row["missing_target"] for row in updated["unresolved"]] == [
        "《外部手册》", "另一项缺失条件"
    ]
    assert len(updated["page_changes"][0]["necessary_context"]) == 1
    assert len(updated["external_references"]) == 1


def test_reference_decision_keeps_broader_or_distinct_blockers():
    from tests.test_document_plan_compiler import _context

    context = replace(
        _context(["遵循《外部操作手册》中的关键参数。"]),
        selection_protocol="document-plan-v5",
    )
    identity = context.parsed.blocks[0].id
    candidate = {
        "page_changes": [{"local_key": "page", "target_key": "p:page",
                          "necessary_context": [], "limitations": []}],
        "unresolved": [
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "外部操作手册中的关键参数",
             "location": [{"from_block": identity, "through_block": identity}],
             "affected_pages": ["page"]},
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "外部操作手册",
             "location": [{"from_block": identity, "through_block": identity}],
             "affected_pages": ["page"]},
            {"problem_type": "unresolved_cross_reference",
             "missing_target": "外部操作手册中的关键参数",
             "location": [{"block": identity, "start_char": 2, "end_char": 10}],
             "affected_pages": ["page"]},
        ],
        "external_references": [],
    }
    reference = ReferenceCandidate(
        "manual", ({"block_index": 0, "start_char": 2, "end_char": 10},),
        "外部操作手册", (), "external_not_supplied", ("page",),
    )
    updated = apply_reference_decisions(
        candidate,
        {("manual", "page"): {"decision": "informational", "reason": "仅引用"}},
        [reference], resolver=SelectionResolver.from_context(context),
    )
    assert updated["unresolved"] == candidate["unresolved"]








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
