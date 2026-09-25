"""Rejected DocumentPlan candidates must yield actionable repair diagnostics."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from openkb.agent.document_plan_feedback import (
    RepairScopeError,
    authorize_repair,
    retry_messages,
    validation_feedback,
)
from openkb.agent.document_plan_issues import PlanValidationError, ValidationIssue
from openkb.agent.document_plan_repair_state import PlanningRepairSession
from openkb.agent.document_planning_support import canonicalize_context_bases
from openkb.agent.document_protocol import decode_plan_response
from openkb.agent.evidence_wire import WireMessages
from openkb.processing import ProcessingIncomplete
from openkb.sources import content_id


def candidate():
    return {
        "overview": {"text": "A procedure.", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "p1",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/procedure",
                "title": "Procedure",
                "purpose": "Explain the procedure.",
                "subject_ranges": [[0, 1]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }


def decode(value, *, total_blocks=1):
    return decode_plan_response(
        value,
        target_start=0,
        target_end=total_blocks,
        total_blocks=total_blocks,
        allowed_entity_types=["product"],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
        block_chars=[15] * total_blocks,
    )


def feedback(value, error, *, total_blocks=1):
    return validation_feedback(
        value,
        error,
        total_blocks=total_blocks,
        block_chars=[15] * total_blocks,
        ignored_blocks=set(),
    )


def test_rejected_concept_type_names_the_actual_field():
    value = candidate()
    value["page_changes"][0]["type"] = "product"
    with pytest.raises(ValueError) as rejected:
        decode(value)

    result = feedback(value, rejected.value)
    assert any(
        issue["field"] == "page_changes[0].type" and issue["code"] == "invalid_entity_type"
        for issue in result["issues"]
    )


def test_empty_subject_range_names_the_reselectable_field():
    value = candidate()
    value["page_changes"][0]["subject_ranges"] = []
    with pytest.raises(ValueError) as rejected:
        decode(value)
    result = feedback(value, rejected.value)
    assert result["issues"][0]["code"] == "range_empty"
    assert result["issues"][0]["field"] == "page_changes[0].subject_ranges"


def test_v2_context_quote_is_derived_from_frozen_evidence_not_model_rationale():
    value = candidate()
    value["page_changes"][0]["necessary_context"] = [
        {
            "relation": "explicit_reference",
            "ranges": [[0, 1]],
            "basis_ranges": [[0, 1]],
            "rationale": "This is an explanation, not an exact quotation.",
        }
    ]
    decoded = decode_plan_response(
        value,
        target_start=0,
        target_end=1,
        total_blocks=1,
        allowed_entity_types=[],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
        block_chars=[15],
        context_contract="document-plan-v2",
    )
    context = decoded["page_changes"][0]["necessary_context"][0]
    canonicalize_context_bases(
        decoded,
        {"blocks": [{"order": 0, "text": "Exact original."}]},
        SimpleNamespace(blocks=[SimpleNamespace(chars=15)]),
    )
    assert context["rationale"] == "This is an explanation, not an exact quotation."
    assert context["basis_quote"] == "Exact original."
    assert context["basis"] == "Exact original."


def test_v2_response_rejects_legacy_model_basis_field():
    value = candidate()
    value["page_changes"][0]["necessary_context"] = [
        {
            "relation": "explicit_reference",
            "ranges": [[0, 1]],
            "basis_ranges": [[0, 1]],
            "basis": "Model copied prose",
        }
    ]
    with pytest.raises(ValueError) as rejected:
        decode_plan_response(
            value,
            target_start=0,
            target_end=1,
            total_blocks=1,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
            context_contract="document-plan-v2",
        )
    result = feedback(value, rejected.value)
    assert result["issues"][0]["field"] == "page_changes[0].necessary_context[0].basis"


def test_model_rationale_remains_in_plan_but_not_generation_or_review_input():
    from openkb.agent.document_page_contracts import _page_input
    from openkb.agent.document_pages import _page_fields

    context = {
        "relation": "explicit_reference",
        "ranges": [[0, 1]],
        "basis_ranges": [[0, 1]],
        "basis": "Exact original.",
        "basis_quote": "Exact original.",
        "rationale": "Model explanation may be unsupported.",
    }
    page = SimpleNamespace(
        key="p1",
        kind="concept",
        type=None,
        name="concepts/example",
        title="Example",
        purpose="Explain the procedure",
        target="",
        subject_ranges=[[0, 1]],
        necessary_context=[context],
        limitations=[],
    )
    assert page.necessary_context[0]["rationale"] == context["rationale"]
    for projected in (_page_input(page), _page_fields(page)):
        assert projected["necessary_context"][0]["basis_quote"] == "Exact original."
        assert "rationale" not in projected["necessary_context"][0]


def test_prompt_bounds_quote_hint_but_failure_record_retains_full_diagnostic():
    quote = "A" * 2_000
    issue = ValidationIssue(
        code="basis_quote_mismatch",
        path="page_changes[0].necessary_context[0].basis",
        category="evidence",
        expected=quote,
        actual="Paraphrase",
        source_ranges=[[0, 1]],
        allowed_action="quote_repair",
    )
    result = feedback(candidate(), PlanValidationError("mismatch", [issue]))
    assert len(result["issues"][0]["expected"]) <= 512
    assert result["all_issues"][0]["expected"] == quote


def test_unknown_affected_page_names_reference_field():
    value = candidate()
    value["unresolved"] = [
        {
            "location": [[0, 1]],
            "problem_type": "missing_external_material",
            "missing_target": "External guide",
            "affected_pages": ["missing"],
            "blocking": True,
            "reason": "Required instructions not supplied.",
        }
    ]
    with pytest.raises(ValueError) as rejected:
        decode(value)
    assert any(
        issue["code"] == "unknown_page_reference"
        and issue["field"] == "unresolved[0].affected_pages[0]"
        for issue in feedback(value, rejected.value)["issues"]
    )


def test_uncovered_target_reports_the_missing_position():
    value = candidate()
    value["overview"]["ranges"] = [[0, 2]]
    with pytest.raises(ValueError) as rejected:
        decode(value, total_blocks=2)
    assert any(
        issue["code"] == "coverage_gap" and issue["field"] == "coverage[1]"
        for issue in feedback(value, rejected.value, total_blocks=2)["issues"]
    )


def test_empty_range_is_classified_at_its_actual_path():
    value = candidate()
    value["page_changes"][0]["subject_ranges"] = [[0, 0]]
    with pytest.raises(ValueError) as rejected:
        decode(value)
    assert any(
        issue["code"] == "range_empty" and issue["field"] == "page_changes[0].subject_ranges[0]"
        for issue in feedback(value, rejected.value)["issues"]
    )


def test_source_only_cannot_also_be_a_page_subject():
    value = candidate()
    value["source_only"] = [{"ranges": [[0, 1]], "reason": "Keep only with original."}]
    with pytest.raises(ValueError) as rejected:
        decode(value)
    assert any(
        issue["code"] == "source_only_conflict" and issue["field"] == "source_only[0].ranges[0]"
        for issue in feedback(value, rejected.value)["issues"]
    )


def test_wrong_original_quote_names_basis_and_supplied_range():
    value = candidate()
    value["page_changes"][0]["necessary_context"] = [
        {
            "relation": "applicable_condition",
            "ranges": [[0, 1]],
            "basis": "A paraphrase.",
            "basis_ranges": [[0, 1]],
        }
    ]
    decoded = decode(value)
    evidence = {"blocks": [{"order": 0, "text": "Exact original.", "reference": {}}]}
    parsed = SimpleNamespace(blocks=[SimpleNamespace(chars=len("Exact original."))])
    with pytest.raises(ValueError) as rejected:
        canonicalize_context_bases(decoded, evidence, parsed)
    assert any(
        issue["code"] == "basis_quote_mismatch"
        and issue["field"] == "page_changes[0].necessary_context[0].basis"
        and issue["source_ranges"] == [[0, 1]]
        for issue in feedback(value, rejected.value)["issues"]
    )


def test_retry_preserves_the_rejected_candidate_and_frozen_prefix():
    value = candidate()
    value["page_changes"][0]["type"] = "product"
    with pytest.raises(ValueError) as rejected:
        decode(value)
    original = {
        "evidence": {"blocks": [{"order": 0, "text": "Exact original."}]},
        "stage": "planning",
    }
    messages = WireMessages(
        [
            {"role": "system", "content": "System"},
            {"role": "user", "content": json.dumps(original)},
        ],
        {},
    )
    revised, _ = retry_messages(
        messages,
        value,
        rejected.value,
        {"total_blocks": 1, "block_chars": [15], "ignored_blocks": set()},
    )
    payload = json.loads(revised[-1]["content"])
    assert payload["repair_request"]["rejected_candidate"] == value
    assert payload["repair_request"]["candidate_hash"]
    assert payload["repair_request"]["allowed_changes"] == ["page_changes[0].type"]
    assert payload["evidence"] == original["evidence"]
    assert messages[-1]["content"] == json.dumps(original)


def test_syntax_error_is_specific_and_can_retain_the_original_text():
    malformed = json.dumps(candidate()) + "]"
    with pytest.raises(ValueError) as rejected:
        decode(malformed)
    issues = feedback(malformed, rejected.value)["issues"]
    assert issues[0]["code"] == "json_syntax"
    assert issues[0]["field"] == "$"
    assert issues[0]["line"] == 1


def test_duplicate_json_field_is_rejected_with_its_path():
    raw = json.dumps(candidate()).replace(
        '"text": "A procedure."',
        '"text": "A procedure.", "text": "Different value"',
        1,
    )
    with pytest.raises(ValueError) as rejected:
        decode(raw)
    issues = feedback(raw, rejected.value)["issues"]
    assert issues[0]["code"] == "json_duplicate_field"
    assert issues[0]["field"] == "overview.text"


def test_existing_candidate_is_not_mutated_by_failed_decode():
    value = candidate()
    value["page_changes"][0]["type"] = "product"
    original = deepcopy(value)
    with pytest.raises(ValueError):
        decode(value)
    assert value == original


def test_quote_repair_cannot_change_the_page_or_evidence_ranges():
    original = candidate()
    original["page_changes"][0]["necessary_context"] = [
        {
            "relation": "applicable_condition",
            "ranges": [[0, 1]],
            "basis": "Paraphrase",
            "basis_ranges": [[0, 1]],
        }
    ]
    request = {
        "mode": "quote_repair",
        "candidate_hash": content_id(original),
        "rejected_candidate": original,
        "allowed_changes": ["page_changes[0].necessary_context[0].basis"],
    }
    corrected = deepcopy(original)
    corrected["page_changes"][0]["necessary_context"][0]["basis"] = "Exact original."
    authorize_repair(request, corrected)
    corrected["page_changes"][0]["name"] = "concepts/other"
    with pytest.raises(ValueError, match="outside repair scope"):
        authorize_repair(request, corrected)


def test_syntax_repair_preserves_all_field_values():
    raw = '{"overview":{},"page_changes":[]} ]'
    request = {
        "mode": "syntax_repair",
        "candidate_hash": content_id(raw),
        "rejected_candidate": raw,
        "allowed_changes": ["$"],
    }
    authorize_repair(request, '{"overview":{},"page_changes":[]}')
    with pytest.raises(ValueError, match="outside repair scope"):
        authorize_repair(request, '{"overview":{},"page_changes":[1]}')


def test_syntax_repair_accepts_equivalent_string_escape_but_rejects_changed_value():
    raw = '{"overview":{"text":"A\\u0026B"}} }'
    request = {
        "mode": "syntax_repair",
        "candidate_hash": content_id(raw),
        "rejected_candidate": raw,
        "allowed_changes": ["$"],
    }
    authorize_repair(request, ' \n {"overview":{"text":"A&B"}} \n ')
    with pytest.raises(ValueError, match="outside repair scope"):
        authorize_repair(request, '{"overview":{"text":"A and B"}}')


def test_identical_duplicate_key_can_be_removed_without_changing_the_object():
    raw = '{"overview":{"text":"same","text":"same"}}'
    request = {
        "mode": "syntax_repair",
        "candidate_hash": content_id(raw),
        "rejected_candidate": raw,
        "allowed_changes": ["overview.text"],
        "issues": [{"code": "json_duplicate_field", "allowed_action": "syntax_repair"}],
    }
    authorize_repair(request, '{"overview":{"text":"same"}}')
    with pytest.raises(ValueError, match="outside repair scope"):
        authorize_repair(request, '{"overview":{"text":"changed"}}')


def test_field_repair_is_bound_to_candidate_hash_and_named_field():
    original = candidate()
    original["page_changes"][0]["type"] = "product"
    request = {
        "mode": "field_repair",
        "candidate_hash": content_id(original),
        "rejected_candidate": original,
        "allowed_changes": ["page_changes[0].type"],
    }
    corrected = deepcopy(original)
    corrected["page_changes"][0].pop("type")
    authorize_repair(request, corrected)
    corrected["page_changes"][0]["name"] = "concepts/other"
    with pytest.raises(ValueError, match="outside repair scope"):
        authorize_repair(request, corrected)
    request["candidate_hash"] = "0" * 64
    with pytest.raises(ValueError, match="candidate identity"):
        authorize_repair(request, corrected)


def test_conflict_repair_authorizes_its_object_children_but_not_other_sections():
    original = {
        "source_only": [{"ranges": [[0, 1]], "reason": "Original"}],
        "overview": {"text": "A"},
    }
    request = {
        "mode": "field_repair",
        "candidate_hash": content_id(original),
        "rejected_candidate": original,
        "allowed_changes": ["source_only[0]"],
    }
    corrected = deepcopy(original)
    corrected["source_only"][0]["reason"] = "Revised"
    authorize_repair(request, corrected)
    corrected["overview"]["text"] = "Changed"
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        authorize_repair(request, corrected)


def test_conflict_repair_collects_all_items_and_revalidates_complete_candidate():
    value = candidate()
    value["source_only"] = [
        {"ranges": [[0, 1]], "reason": "First conflicting route"},
        {"ranges": [[0, 1]], "reason": "Second conflicting route"},
    ]
    with pytest.raises(ValueError) as rejected:
        decode(value)
    issues = feedback(value, rejected.value)["all_issues"]
    assert [issue["field"] for issue in issues if issue["code"] == "source_only_conflict"] == [
        "source_only[0].ranges[0]",
        "source_only[1].ranges[0]",
    ]
    messages = WireMessages(
        [
            {"role": "system", "content": "System"},
            {"role": "user", "content": '{"stage":"planning"}'},
        ],
        {},
    )
    revised, _ = retry_messages(
        messages,
        value,
        rejected.value,
        {"total_blocks": 1, "block_chars": [15], "ignored_blocks": set()},
    )
    repair = json.loads(revised[-1]["content"])["repair_request"]
    assert set(repair["allowed_changes"]) >= {
        "source_only[0]",
        "source_only[1]",
        "page_changes[0].subject_ranges",
    }
    corrected = deepcopy(value)
    corrected["source_only"] = []
    authorize_repair(repair, corrected)
    decode(corrected)
    corrected["overview"]["text"] = "Unrelated rewrite"
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        authorize_repair(repair, corrected)


def test_source_only_deletion_preserves_unmodified_siblings_by_content():
    original = {
        "source_only": [
            {"ranges": [[0, 1]], "reason": "Remove"},
            {"ranges": [[1, 2]], "reason": "Keep"},
        ]
    }
    request = {
        "mode": "field_repair",
        "candidate_hash": content_id(original),
        "rejected_candidate": original,
        "allowed_changes": ["source_only[0]"],
    }
    corrected = {"source_only": [deepcopy(original["source_only"][1])]}
    authorize_repair(request, corrected)
    corrected["source_only"][0]["reason"] = "Changed keep"
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        authorize_repair(request, corrected)


def test_pending_repair_resumes_same_candidate_and_stops_without_progress():
    class Checkpoints:
        input = {"source": "test"}

        def __init__(self):
            self.saved = {}

        def load_recovery(self, key, kind):
            return self.saved.get((key, kind))

        def save_recovery(self, key, kind, value):
            self.saved[key, kind] = value

    value = candidate()
    value["page_changes"][0]["type"] = "product"
    with pytest.raises(ValueError) as rejected:
        decode(value)
    messages = WireMessages(
        [
            {"role": "system", "content": "System"},
            {"role": "user", "content": '{"stage":"planning"}'},
        ],
        {},
    )
    kwargs = {"total_blocks": 1, "block_chars": [15], "ignored_blocks": set()}
    limits = SimpleNamespace(max_attempts=3, input_capacity=100_000)
    checkpoints = Checkpoints()
    first = PlanningRepairSession(messages, kwargs, checkpoints, {"window_id": "w1"}, "s1", limits)
    first.invalid(value, rejected.value, 0)
    resumed = PlanningRepairSession(
        messages, kwargs, checkpoints, {"window_id": "w1"}, "s1", limits
    )
    assert resumed.next_attempt == 1
    assert (
        json.loads(resumed.messages[-1]["content"])["repair_request"]["rejected_candidate"] == value
    )
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        resumed.invalid(value, rejected.value, 1)
    assert checkpoints.saved[first.key, "plan_repair"]["next_attempt"] == limits.max_attempts


def test_out_of_scope_repair_is_durably_rejected_without_another_request():
    class Checkpoints:
        input = {"source": "test"}

        def __init__(self):
            self.saved = {}

        def load_recovery(self, key, kind):
            return self.saved.get((key, kind))

        def save_recovery(self, key, kind, value):
            self.saved[key, kind] = value

    messages = WireMessages(
        [
            {"role": "system", "content": "System"},
            {"role": "user", "content": '{"stage":"planning"}'},
        ],
        {},
    )
    checkpoints = Checkpoints()
    limits = SimpleNamespace(max_attempts=3, input_capacity=100_000)
    session = PlanningRepairSession(
        messages,
        {"total_blocks": 1, "block_chars": [15], "ignored_blocks": set()},
        checkpoints,
        {"window_id": "w1"},
        "s1",
        limits,
    )
    corrected = candidate()
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        session.invalid(corrected, RepairScopeError("secret exception detail"), 1)
    record = checkpoints.saved[session.key, "plan_repair"]
    assert record["candidate"] == corrected
    assert record["diagnostics"]["issues"][0]["code"] == "repair_scope_violation"
    assert "secret exception detail" not in json.dumps(record)
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        PlanningRepairSession(
            messages, session.decode_kwargs, checkpoints, {"window_id": "w1"}, "s1", limits
        )


def test_scope_failure_record_keeps_safe_changed_and_authorized_paths():
    class Checkpoints:
        input = {"source": "test"}

        def __init__(self):
            self.saved = {}

        def load_recovery(self, key, kind):
            return self.saved.get((key, kind))

        def save_recovery(self, key, kind, value):
            self.saved[key, kind] = value

    original = candidate()
    original["source_only"] = [{"ranges": [[0, 1]], "reason": "Conflicting"}]
    request = {
        "mode": "field_repair",
        "rejected_candidate": original,
        "candidate_hash": content_id(original),
        "allowed_changes": ["source_only[0]"],
    }
    corrected = deepcopy(original)
    corrected["overview"]["text"] = "Unrelated rewrite"
    with pytest.raises(RepairScopeError) as rejected:
        authorize_repair(request, corrected)
    messages = WireMessages(
        [
            {"role": "system", "content": "System"},
            {
                "role": "user",
                "content": json.dumps({"stage": "planning", "repair_request": request}),
            },
        ],
        {},
    )
    checkpoints = Checkpoints()
    session = PlanningRepairSession(
        messages,
        {"total_blocks": 1, "block_chars": [15], "ignored_blocks": set()},
        checkpoints,
        {"window_id": "w1"},
        "s1",
        SimpleNamespace(max_attempts=3, input_capacity=100_000),
    )
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        session.invalid(corrected, rejected.value, 1)
    record = checkpoints.saved[session.key, "plan_repair"]
    assert record["diagnostics"]["issues"][0]["field"] == "overview.text"
    assert record["diagnostics"]["allowed_changes"] == ["source_only[0]"]
