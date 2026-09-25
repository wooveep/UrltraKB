"""Authorization, stable item identity, and pending v3 recovery."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openkb.agent.document_plan_compiler import compile_plan_candidate
from openkb.agent.document_plan_feedback import RepairScopeError
from openkb.agent.document_plan_issues import ValidationIssue
from openkb.agent.document_plan_patches import (
    apply_plan_patch,
    compact_patch_request,
    item_refs,
    patch_request,
)
from openkb.agent.document_plan_repair_state import V3PlanningRepairSession
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.evidence_wire import WireMessages
from openkb.processing import ProcessingIncomplete
from tests.test_document_plan_compiler import _candidate, _context, _page


def _operation(request, op, field, value=None):
    grant = next(
        row for row in request["allowed_operations"] if row["op"] == op and row["field"] == field
    )
    result = {
        "issue_id": grant["issue_id"],
        "op": op,
        "item_ref": grant["item_ref"],
    }
    if op != "remove_item":
        result["field"] = field
    if op in {"replace_field", "append_item"}:
        result["value"] = value
    return result


def _patch(request, operations):
    return {
        "repair_protocol": request["repair_protocol"],
        "candidate_hash": request["candidate_hash"],
        "operations": operations,
    }


def _whole(resolver, first, last):
    return resolver.encode_ranges([[first, last + 1]])[0]


def test_multiple_invalid_selections_grant_one_field_replacement():
    candidate = _candidate(
        _page(),
        unresolved=[
            {
                "location": [{"from_block": "incomplete"}],
                "problem_type": "missing_external_material",
                "missing_target": "manual",
                "affected_pages": ["c1"],
                "reason": "needed",
            }
        ],
    )
    issues = (
        ValidationIssue(
            "invalid_selection_shape", "unresolved[0].location[0]", "evidence", "complete selection"
        ),
        ValidationIssue("range_empty", "unresolved[0].location", "evidence", "nonempty range"),
    )
    request = patch_request(
        candidate, item_refs(candidate), issues, protocol="document-plan-repair-v2"
    )
    grants = [
        row
        for row in request["allowed_operations"]
        if row["op"] == "replace_field" and row["field"] == "location"
    ]
    assert len(grants) == 1
    assert len(grants[0]["related_issue_ids"]) == 2


def test_gap_patch_rejects_wrong_hash_overview_edit_and_atomic_mixed_batch():
    context = _context(["condition", "procedure"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    issues = compile_plan_candidate(baseline, context).issues
    refs = item_refs(baseline)
    original_refs = deepcopy(refs)
    request = patch_request(baseline, refs, issues)
    append = _operation(
        request,
        "append_item",
        "source_only",
        {"ranges": [[0, 1]], "reason": "Version note"},
    )
    wrong_hash = _patch(request, [append])
    wrong_hash["candidate_hash"] = "0" * 64
    with pytest.raises(RepairScopeError, match="identity"):
        apply_plan_patch(baseline, refs, request, wrong_hash, block_chars=[9, 9])
    assert not any(row["item_ref"] == "overview" for row in request["allowed_operations"])
    overview = {
        "issue_id": append["issue_id"],
        "op": "replace_field",
        "item_ref": "overview",
        "field": "text",
        "value": "Unrelated rewrite",
    }
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        apply_plan_patch(
            baseline,
            refs,
            request,
            _patch(request, [overview]),
            block_chars=[9, 9],
        )
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        apply_plan_patch(
            baseline,
            refs,
            request,
            _patch(request, [append, overview]),
            block_chars=[9, 9],
        )
    assert baseline["source_only"] == []
    assert refs == original_refs


def test_compact_repair_request_keeps_grants_and_server_side_candidate():
    context = _context(["metadata", "body"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    refs = item_refs(baseline)
    full = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    compact = compact_patch_request(full, issue_limit=1)
    assert "candidate" not in compact and "item_refs" not in compact
    assert compact["candidate_hash"] == full["candidate_hash"]
    assert compact["candidate_projection"]["items"][0]["value"]["title"] == "中文标题"
    operation = _operation(
        compact,
        "append_item",
        "source_only",
        {"ranges": [[0, 1]], "reason": "Metadata only"},
    )
    patched = apply_plan_patch(
        baseline, refs, compact, _patch(compact, [operation]), block_chars=[8, 4]
    )
    assert compile_plan_candidate(patched.candidate, context).delta is not None


def test_program_owned_name_can_be_removed_but_legal_field_cannot():
    context = _context(["procedure"])
    baseline = _candidate(_page())
    baseline["page_changes"][0]["name"] = "concepts/model-choice"
    refs = item_refs(baseline)
    request = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    remove_name = _operation(request, "remove_field", "name")
    patched = apply_plan_patch(
        baseline, refs, request, _patch(request, [remove_name]), block_chars=[9]
    )
    result = compile_plan_candidate(patched.candidate, context)
    assert result.delta["page_changes"][0]["name"].startswith("concepts/page-")
    illegal = {
        **remove_name,
        "field": "purpose",
    }
    with pytest.raises(RepairScopeError):
        apply_plan_patch(baseline, refs, request, _patch(request, [illegal]), block_chars=[9])


def test_invalid_optional_concept_type_can_be_removed():
    context = _context(["procedure"])
    baseline = _candidate(_page())
    baseline["page_changes"][0]["type"] = "not applicable"
    refs = item_refs(baseline)
    request = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    remove = _operation(request, "remove_field", "type")
    patched = apply_plan_patch(baseline, refs, request, _patch(request, [remove]), block_chars=[9])
    assert "type" not in patched.candidate["page_changes"][0]
    assert compile_plan_candidate(patched.candidate, context).accepted


def test_removing_first_source_only_item_keeps_later_refs_and_content():
    context = _context(["body", "note A", "note B"])
    baseline = _candidate(
        _page(subject_ranges=[[0, 1]]),
        overview_ranges=[[0, 3]],
        source_only=[
            {"ranges": [[0, 1]], "reason": "conflict"},
            {"ranges": [[1, 2]], "reason": "note A"},
            {"ranges": [[2, 3]], "reason": "note B"},
        ],
    )
    refs = item_refs(baseline)
    trailing = refs["sections"]["source_only"][1:]
    request = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    remove = _operation(request, "remove_item", None)
    patched = apply_plan_patch(
        baseline, refs, request, _patch(request, [remove]), block_chars=[4, 6, 6]
    )
    assert patched.refs["sections"]["source_only"] == trailing
    assert patched.candidate["source_only"] == baseline["source_only"][1:]
    assert compile_plan_candidate(patched.candidate, context).delta is not None
    assert refs["sections"]["source_only"][0] == remove["item_ref"]


def test_one_conflicting_range_across_blocks_has_one_field_repair():
    context = _context(["meta", "body", "more", "tail"])
    baseline = _candidate(
        _page(subject_ranges=[[1, 4]]),
        overview_ranges=[[0, 4]],
        source_only=[{"ranges": [[0, 1], [2, 4]], "reason": "metadata and appendix"}],
    )
    result = compile_plan_candidate(baseline, context)
    conflicts = [issue for issue in result.issues if issue.code == "source_only_conflict"]
    assert len(conflicts) == 1
    assert {row["block_index"] for row in conflicts[0].source_ranges} == {2, 3}
    refs = item_refs(baseline)
    request = patch_request(baseline, refs, result.issues)
    operation = _operation(request, "replace_field", "ranges", [[0, 1]])
    patched = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(request, [operation]),
        block_chars=[4, 4, 4, 4],
    )
    assert compile_plan_candidate(patched.candidate, context).delta is not None


def test_missing_context_does_not_hide_independent_source_conflicts():
    context = _context(["metadata", "body A", "body B"])
    candidate = _candidate(
        _page(subject_ranges=[[1, 3]]),
        overview_ranges=[[0, 3]],
        source_only=[{"ranges": [[0, 1], [1, 2], [2, 3]], "reason": "Metadata"}],
    )
    del candidate["page_changes"][0]["necessary_context"]
    result = compile_plan_candidate(candidate, context)
    assert {"missing_field", "source_only_conflict"} <= {row.code for row in result.issues}
    assert result.delta is None


def test_one_replacement_can_remove_two_diagnosed_conflicts():
    texts = ["metadata", "body A", "body B"]
    context = _context(texts)
    candidate = _candidate(
        _page(subject_ranges=[[1, 3]]),
        overview_ranges=[[0, 3]],
        source_only=[{"ranges": [[0, 1], [1, 2], [2, 3]], "reason": "Metadata"}],
    )
    refs = item_refs(candidate)
    request = patch_request(candidate, refs, compile_plan_candidate(candidate, context).issues)
    grants = [
        row
        for row in request["allowed_operations"]
        if row["op"] == "replace_field" and row["field"] == "ranges"
    ]
    assert len(grants) == 1
    assert len(grants[0]["related_issue_ids"]) == 2
    operation = _operation(request, "replace_field", "ranges", [[0, 1]])
    patched = apply_plan_patch(
        candidate, refs, request, _patch(request, [operation]), block_chars=[8, 6, 6]
    )
    assert patched.candidate["source_only"][0]["ranges"] == [[0, 1]]
    assert compile_plan_candidate(patched.candidate, context).accepted


def test_two_distinct_appends_share_array_and_get_stable_refs():
    texts = ["version", "body", "provenance"]
    context = _context(texts)
    candidate = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 3]])
    refs = item_refs(candidate)
    request = patch_request(candidate, refs, compile_plan_candidate(candidate, context).issues)
    operations = [
        _operation(request, "append_item", "source_only", value)
        for value in (
            {"ranges": [[0, 1]], "reason": "Version metadata"},
            {"ranges": [[2, 3]], "reason": "Provenance metadata"},
        )
    ]
    patched = apply_plan_patch(
        candidate, refs, request, _patch(request, operations), block_chars=[7, 4, 10]
    )
    assert patched.candidate["source_only"] == [row["value"] for row in operations]
    assert len(set(patched.refs["sections"]["source_only"])) == 2
    assert compile_plan_candidate(patched.candidate, context).accepted


def test_patch_contract_and_safe_content_alias():
    context = _context(["note", "body"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    refs = item_refs(baseline)
    request = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    compact = compact_patch_request(request, issue_limit=1)
    for shown in (request, compact):
        contract = shown["response_contract"]
        assert contract["top_level_fields"] == ["repair_protocol", "candidate_hash", "operations"]
        assert "value" in contract["operations"]["replace_field"]["required_fields"]
        assert "value" in contract["operations"]["append_item"]["required_fields"]
        assert "value" not in contract["operations"]["remove_field"]["required_fields"]
        assert "value" not in contract["operations"]["remove_item"]["required_fields"]
        assert "necessary_context" in contract["value_shapes"]["page_changes"]["required_fields"]
        assert contract["value_shapes"]["necessary_context"]["required_fields"] == [
            "basis_ranges",
            "ranges",
            "relation",
        ]
        assert "response_mode=patch" in shown["instruction"]
        assert any(row["value_type"].startswith("object:") for row in shown["allowed_operations"])
    append = _operation(
        request, "append_item", "source_only", {"ranges": [[0, 1]], "reason": "Note"}
    )
    append["content"] = append.pop("value")
    result = apply_plan_patch(
        baseline, refs, request, _patch(request, [append]), block_chars=[4, 4]
    )
    assert result.normalizations[0]["conversion"] == "content_to_value"
    assert compile_plan_candidate(result.candidate, context).accepted
    assert baseline["source_only"] == []

    missing = deepcopy(baseline)
    del missing["page_changes"][0]["necessary_context"]
    missing_refs = item_refs(missing)
    missing_request = patch_request(
        missing, missing_refs, compile_plan_candidate(missing, context).issues
    )
    replace = _operation(missing_request, "replace_field", "necessary_context", [])
    replace["content"] = replace.pop("value")
    filled = apply_plan_patch(
        missing,
        missing_refs,
        missing_request,
        _patch(missing_request, [replace]),
        block_chars=[4, 4],
    )
    assert filled.candidate["page_changes"][0]["necessary_context"] == []
    assert filled.normalizations[0]["operation_index"] == 0


@pytest.mark.parametrize("change", ["both", "unknown", "wrong_type", "wrong_op", "wrong_hash"])
def test_ambiguous_or_unauthorized_alias_is_atomic(change):
    context = _context(["note", "body"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    refs = item_refs(baseline)
    previous_refs = deepcopy(refs)
    request = patch_request(baseline, refs, compile_plan_candidate(baseline, context).issues)
    append = _operation(
        request, "append_item", "source_only", {"ranges": [[0, 1]], "reason": "Note"}
    )
    if change == "both":
        append["content"] = append["value"]
    elif change == "unknown":
        append["data"] = append.pop("value")
    elif change == "wrong_type":
        append["value"] = []
    elif change == "wrong_op":
        append["op"] = "replace_item"
    patch = _patch(request, [append])
    if change == "wrong_hash":
        patch["candidate_hash"] = "0" * 64
    with pytest.raises(RepairScopeError):
        apply_plan_patch(baseline, refs, request, patch, block_chars=[4, 4])
    assert baseline["source_only"] == []
    assert refs == previous_refs


def test_duplicate_append_and_nonconflicting_remove_are_rejected():
    context = _context(["metadata", "body"])
    candidate = _candidate(
        _page(subject_ranges=[[1, 2]]),
        overview_ranges=[[0, 2]],
        source_only=[{"ranges": [[0, 2]], "reason": "Mixed"}],
    )
    refs = item_refs(candidate)
    request = patch_request(candidate, refs, compile_plan_candidate(candidate, context).issues)
    remove = _operation(request, "remove_item", None)
    with pytest.raises(RepairScopeError, match="nonconflicting"):
        apply_plan_patch(candidate, refs, request, _patch(request, [remove]), block_chars=[8, 4])
    replace = _operation(request, "replace_field", "ranges", [])
    with pytest.raises(RepairScopeError, match="diagnosed overlap"):
        apply_plan_patch(candidate, refs, request, _patch(request, [replace]), block_chars=[8, 4])

    candidate = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    refs = item_refs(candidate)
    request = patch_request(candidate, refs, compile_plan_candidate(candidate, context).issues)
    append = _operation(
        request, "append_item", "source_only", {"ranges": [[0, 1]], "reason": "Metadata"}
    )
    with pytest.raises(RepairScopeError, match="Duplicate append"):
        apply_plan_patch(
            candidate, refs, request, _patch(request, [append, append]), block_chars=[8, 4]
        )
    replace_subject = _operation(request, "replace_field", "subject_ranges", [[0, 2]])
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        apply_plan_patch(
            candidate,
            refs,
            request,
            _patch(request, [replace_subject, replace_subject]),
            block_chars=[8, 4],
        )
    assert candidate["source_only"] == []


class _Recovery:
    input = {"version": "test"}

    def __init__(self):
        self.records = {}

    def load_recovery(self, key, kind):
        return self.records.get((key, kind))

    def save_recovery(self, key, kind, value):
        self.records[key, kind] = deepcopy(value)


def _messages(protocol="document-plan-v3"):
    return WireMessages(
        [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps({"stage": "planning", "plan_protocol": protocol}),
            },
        ],
        {},
    )


def test_pending_v3_replays_saved_patch_and_stops_on_no_progress():
    context = _context(["note", "body"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    compiled = compile_plan_candidate(baseline, context)
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=3, input_capacity=100_000)
    session = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4, 4]
    )
    session.invalid(baseline, compiled, 0)
    request = session.repair_request
    response = _patch(
        request,
        [
            _operation(
                request,
                "append_item",
                "source_only",
                {"ranges": [[0, 1]], "reason": "Version note"},
            )
        ],
    )
    response["operations"][0]["content"] = response["operations"][0].pop("value")
    session.record_response(json.dumps(response), "request-key")
    resumed = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4, 4]
    )
    assert resumed.next_attempt == 1
    assert json.loads(resumed.pending_response) == response
    patched = resumed.apply_response(resumed.pending_response)
    compiled_patch = compile_plan_candidate(patched, context)
    assert compiled_patch.delta is not None
    assert resumed.next_attempt == 1
    resumed.applied(patched)
    resumed.accepted(patched, compiled_patch.delta)
    receipt = records.records[session.key, "plan_repair"]
    assert json.loads(receipt["last_response"]) == response
    assert receipt["repair_response_hash"]
    assert receipt["final_candidate_hash"]
    assert receipt["normalization"][0]["conversion"] == "content_to_value"
    assert receipt["normalization"][0]["normalized_response_hash"]
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        resumed.invalid(baseline, compiled, 1)
    assert records.records[session.key, "plan_repair"]["next_attempt"] == 3


def test_empty_patch_records_no_progress_without_acceptance():
    context = _context(["note", "body"])
    baseline = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 2]])
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    session = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4, 4]
    )
    session.invalid(baseline, compile_plan_candidate(baseline, context), 0)
    empty = _patch(session.repair_request, [])
    session.record_response(json.dumps(empty), "request-key")
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        session.apply_response(json.dumps(empty))
    record = records.records[session.key, "plan_repair"]
    assert record["stop_reason"] == "no_progress"
    assert record["next_attempt"] == 2
    assert not record.get("accepted")


def test_non_object_candidate_stops_with_unchecked_diagnostic():
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    session = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4]
    )
    result = compile_plan_candidate([], _context(["body"]))
    assert result.coverage_status == "unchecked"
    with pytest.raises(ProcessingIncomplete, match="document_plan_invalid"):
        session.invalid([], result, 0)
    record = records.records[session.key, "plan_repair"]
    assert record["stop_reason"] == "no_authorized_repair"
    assert record["candidate"] == []


def test_bad_source_claim_has_explicit_discard_route():
    context = replace(_context(["heading", "body", "tail"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 2)]),
        overview_ranges=[_whole(resolver, 0, 2)],
        source_only=[
            {
                "ranges": [{"from_block": "unknown", "through_block": "unknown"}],
                "reason": "stale claim",
            }
        ],
    )
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
        target_ranges=[[0, 3]],
        editable_page_refs=refs["sections"]["page_changes"],
    )
    grant = next(row for row in request["allowed_operations"] if row["op"] == "resolve_source_only")
    operation = {
        "issue_id": grant["issue_id"],
        "op": "resolve_source_only",
        "item_ref": grant["item_ref"],
        "value": {"decision": "discard_claim"},
    }
    result = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(request, [operation]),
        block_chars=[7, 4, 4],
        resolver=resolver,
    )
    assert result.candidate["source_only"] == []
    assert compile_plan_candidate(result.candidate, context).accepted


def test_retain_in_source_can_deduct_exact_current_subject_overlap():
    context = replace(
        _context(["procedure A", "version note", "procedure B"]),
        selection_protocol="document-plan-v4",
    )
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 2)]),
        overview_ranges=[_whole(resolver, 0, 2)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "incorrect claim"}],
    )
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
        target_ranges=[[0, 3]],
        editable_page_refs=refs["sections"]["page_changes"],
    )
    grant = next(row for row in request["allowed_operations"] if row["op"] == "resolve_source_only")
    operation = {
        "issue_id": grant["issue_id"],
        "op": "resolve_source_only",
        "item_ref": grant["item_ref"],
        "value": {
            "decision": "retain_in_source",
            "ranges": [_whole(resolver, 1, 1)],
            "reason": "Version note",
        },
    }
    result = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(request, [operation]),
        block_chars=[11, 12, 11],
        resolver=resolver,
    )
    assert resolver.decode_ranges(
        result.candidate["page_changes"][0]["subject_ranges"],
        "page_changes[0].subject_ranges",
        target_only=True,
    ) == [[0, 1], [2, 3]]
    assert result.candidate["source_only"][0]["ranges"] == [_whole(resolver, 1, 1)]
    assert compile_plan_candidate(result.candidate, context).accepted


def test_source_route_subtracts_characters_from_two_pages_and_rejects_scope_expansion():
    context = replace(_context(["abcdef", "ghijkl", "mnop"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page("a", subject_ranges=[_whole(resolver, 0, 1)], title="Page A"),
        overview_ranges=[_whole(resolver, 0, 2)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "wrong"}],
    )
    baseline["page_changes"].append(
        _page("b", subject_ranges=[_whole(resolver, 1, 2)], title="Page B")
    )
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
        target_ranges=[[0, 3]],
        editable_page_refs=refs["sections"]["page_changes"],
    )
    grant = next(row for row in request["allowed_operations"] if row["op"] == "resolve_source_only")
    selection = resolver.encode_ranges([{"block_index": 1, "start_char": 2, "end_char": 4}])
    operation = {
        "issue_id": grant["issue_id"],
        "op": "resolve_source_only",
        "item_ref": grant["item_ref"],
        "value": {"decision": "retain_in_source", "ranges": selection, "reason": "Source note"},
    }
    patched = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(request, [operation]),
        block_chars=[6, 6, 4],
        resolver=resolver,
    )
    for page in patched.candidate["page_changes"]:
        ranges = resolver.decode_ranges(page["subject_ranges"], "subject", target_only=True)
        assert {"block_index": 1, "start_char": 0, "end_char": 2} in ranges
        assert {"block_index": 1, "start_char": 4, "end_char": 6} in ranges
    assert compile_plan_candidate(patched.candidate, context).accepted
    assert {
        row["item_ref"] for row in patched.derived_changes if row.get("field") == "subject_ranges"
    } == set(refs["sections"]["page_changes"])

    unauthorized = deepcopy(request)
    unauthorized["allowed_operations"] = deepcopy(request["allowed_operations"])
    route_grant = next(
        row for row in unauthorized["allowed_operations"] if row["op"] == "resolve_source_only"
    )
    route_grant["editable_page_refs"] = refs["sections"]["page_changes"][:1]
    with pytest.raises(RepairScopeError) as caught:
        apply_plan_patch(
            baseline,
            refs,
            unauthorized,
            _patch(unauthorized, [operation]),
            block_chars=[6, 6, 4],
            resolver=resolver,
        )
    assert caught.value.code == "route_scope_violation"
    assert baseline["page_changes"][1]["subject_ranges"] == [_whole(resolver, 1, 2)]

    emptying = deepcopy(operation)
    emptying["value"] = {
        "decision": "retain_in_source",
        "ranges": [_whole(resolver, 0, 1)],
        "reason": "Source note",
    }
    with pytest.raises(RepairScopeError) as caught:
        apply_plan_patch(
            baseline,
            refs,
            request,
            _patch(request, [emptying]),
            block_chars=[6, 6, 4],
            resolver=resolver,
        )
    assert caught.value.code == "route_would_empty_page"

    competing = deepcopy(request)
    competing["allowed_operations"] = [
        *deepcopy(request["allowed_operations"]),
        {
            **grant,
            "op": "replace_field",
            "item_ref": refs["sections"]["page_changes"][0],
            "field": "subject_ranges",
        },
    ]
    direct = {
        "issue_id": grant["issue_id"],
        "op": "replace_field",
        "item_ref": refs["sections"]["page_changes"][0],
        "field": "subject_ranges",
        "value": [_whole(resolver, 0, 1)],
    }
    with pytest.raises(RepairScopeError, match="outside repair scope"):
        apply_plan_patch(
            baseline,
            refs,
            competing,
            _patch(competing, [operation, direct]),
            block_chars=[6, 6, 4],
            resolver=resolver,
        )
    assert baseline["page_changes"][0]["subject_ranges"] == [_whole(resolver, 0, 1)]


def test_discard_source_claim_does_not_hide_new_coverage_gap():
    context = replace(_context(["heading", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 1, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
        source_only=[{"ranges": [[261, 262]], "reason": "invalid coordinate"}],
    )
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
        target_ranges=[[0, 2]],
    )
    grant = next(row for row in request["allowed_operations"] if row["op"] == "resolve_source_only")
    operation = {
        "issue_id": grant["issue_id"],
        "op": "resolve_source_only",
        "item_ref": grant["item_ref"],
        "value": {"decision": "discard_claim"},
    }
    patched = apply_plan_patch(
        baseline, refs, request, _patch(request, [operation]), block_chars=[7, 4], resolver=resolver
    )
    compiled = compile_plan_candidate(patched.candidate, context)
    assert not compiled.accepted
    assert any(issue.code == "coverage_gap" for issue in compiled.issues)


def test_v2_unique_related_diagnostic_id_normalizes_to_one_grant():
    context = replace(_context(["body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 0)]),
        overview_ranges=[_whole(resolver, 0, 0)],
    )
    baseline.pop("resolutions")
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
    )
    grant = next(row for row in request["allowed_operations"] if row["field"] == "resolutions")
    alias = next(
        identity for identity in grant["related_issue_ids"] if identity != grant["issue_id"]
    )
    patched = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(
            request,
            [
                {
                    "issue_id": alias,
                    "op": "replace_field",
                    "item_ref": "plan",
                    "field": "resolutions",
                    "value": [],
                }
            ],
        ),
        block_chars=[4],
        resolver=resolver,
    )
    assert patched.candidate["resolutions"] == []
    assert patched.normalizations[0]["conversion"] == "related_issue_to_grant"


def test_v4_rejected_route_resumes_same_candidate_with_actual_attempt_count():
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "wrong"}],
    )
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=3, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    session.invalid(baseline, compile_plan_candidate(baseline, context), 0)
    item = session.repair_request["items"][0]
    decision = {
        "decision_id": item["decision_id"],
        "decision": "route_source_only",
        "retained_pieces": ["piece:unknown"],
        "reason": "Note",
    }
    invalid_response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": session.repair_request["candidate_hash"],
        "decisions": [decision],
    }
    session.record_response(json.dumps(invalid_response), "request-1")
    assert records.records[session.key, "plan_repair"]["response_status"] == "received"
    with pytest.raises(RepairScopeError) as caught:
        session.apply_response(json.dumps(invalid_response))
    assert caught.value.code == "repair_piece_out_of_scope"
    session.rejected(caught.value, 1)
    rejected = records.records[session.key, "plan_repair"]
    assert (rejected["response_status"], rejected["attempts_used"], rejected["next_attempt"]) == (
        "rejected",
        2,
        2,
    )
    resumed = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    assert resumed.pending_response is None
    assert resumed.repair_request["rejected_response"]["code"] == "repair_piece_out_of_scope"
    decision["retained_pieces"] = [item["source_pieces"][0]["piece_ref"]]
    valid_response = {**invalid_response, "decisions": [decision]}
    resumed.record_response(json.dumps(valid_response), "request-2")
    repaired = resumed.apply_response(json.dumps(valid_response))
    compiled = compile_plan_candidate(repaired, context)
    assert compiled.accepted
    resumed.applied(repaired)
    applied = records.records[session.key, "plan_repair"]
    assert (applied["response_status"], applied["attempts_used"]) == ("applied", 3)
    assert applied["derived_changes"]


def test_v4_initial_received_response_replays_without_a_second_dispatch():
    context = replace(_context(["body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    candidate = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 0)]),
        overview_ranges=[_whole(resolver, 0, 0)],
    )
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4], context=context
    )
    session.record_response(json.dumps(candidate), "request-0")
    assert records.records[session.key, "plan_repair"]["response_status"] == "received"
    resumed = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4], context=context
    )
    assert json.loads(resumed.pending_response) == candidate
    compiled, feedback = resumed.evaluate(resumed.pending_response, context, 0)
    assert feedback is None and compiled.accepted
    resumed.accepted(compiled.candidate, compiled.delta)
    record = records.records[session.key, "plan_repair"]
    assert (record["response_status"], record["attempts_used"]) == ("applied", 1)


def test_legacy_received_response_without_wire_marker_is_not_guessed():
    context = replace(_context(["body"]), selection_protocol="document-plan-v4")
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4], context=context
    )
    session.record_response('{"overview":{}}', "request-0")
    record = records.records[session.key, "plan_repair"]
    record.pop("response_representation")
    with pytest.raises(ProcessingIncomplete, match="document_plan_incompatible_recovery"):
        V3PlanningRepairSession(
            messages, records, {"window_id": "w1"}, "before", limits, [4], context=context
        )


def test_v3_routing_received_and_applied_resume_without_reapplying():
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "source"}],
    )
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    session.invalid(baseline, compile_plan_candidate(baseline, context), 0)
    item = session.repair_request["items"][0]
    routed_response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": session.repair_request["candidate_hash"],
        "decisions": [
            {
                "decision_id": item["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [item["source_pieces"][0]["piece_ref"]],
                "reason": "Source owns note",
            }
        ],
    }
    session.record_response(json.dumps(routed_response), "request-1")
    received = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    assert json.loads(received.pending_response) == routed_response
    repaired = received.apply_response(received.pending_response)
    received.applied(repaired)
    applied = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    assert applied.pending_response is None
    assert applied.applied_candidate == repaired
    assert compile_plan_candidate(applied.applied_candidate, context).accepted


@pytest.mark.parametrize("mode", ["patch", "routing"])
def test_empty_repair_answer_keeps_pending_protocol_across_resume(mode):
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    if mode == "routing":
        baseline = _candidate(
            _page(subject_ranges=[_whole(resolver, 0, 1)]),
            overview_ranges=[_whole(resolver, 0, 1)],
            source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "source"}],
        )
    else:
        baseline = _candidate(
            _page(subject_ranges=[_whole(resolver, 1, 1)]),
            overview_ranges=[_whole(resolver, 0, 1)],
        )
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=3, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    session.invalid(baseline, compile_plan_candidate(baseline, context), 0)
    expected_request = session.messages[-1]["content"]
    expected_phase = session.phase
    candidate_hash = session.repair_request["candidate_hash"]
    session.record_response(" ", "request-1")
    session.empty_response(" ", 1)
    resumed = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    assert resumed.phase == expected_phase
    assert resumed.messages[-1]["content"] == expected_request
    assert resumed.repair_request["candidate_hash"] == candidate_hash
    assert resumed.candidate == baseline
    assert resumed.pending_response is None
    assert (resumed.attempts_used, resumed.next_attempt) == (2, 2)
    assert records.records[session.key, "plan_repair"]["response_status"] == "response_empty"


def test_v3_routing_uses_initial_and_one_repair_attempt_only():
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "source"}],
    )
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    session = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    session.invalid(baseline, compile_plan_candidate(baseline, context), 0)
    item = session.repair_request["items"][0]
    bad = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": session.repair_request["candidate_hash"],
        "decisions": [
            {
                "decision_id": item["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": ["piece:outside"],
                "reason": "Wrong piece",
            }
        ],
    }
    session.record_response(json.dumps(bad), "request-1")
    with pytest.raises(RepairScopeError) as error:
        session.apply_response(json.dumps(bad))
    with pytest.raises(ProcessingIncomplete):
        session.rejected(error.value, 1)
    saved = records.records[session.key, "plan_repair"]
    assert saved["attempts_used"] == 2
    assert saved["stop_reason"] == "repair_scope_violation"


def test_old_v2_private_candidate_recompiles_into_v3_without_resetting_attempts(tmp_path):
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 0, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
        source_only=[{"ranges": [_whole(resolver, 0, 0)], "reason": "source"}],
    )
    records = _Recovery()
    records.root = tmp_path
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    messages = _messages("document-plan-v4")
    fresh = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    old_key = "a" * 64
    (tmp_path / "recovery").mkdir()
    (tmp_path / "recovery" / f"{old_key}-plan_repair.json").write_text("private checkpoint")
    old_request = patch_request(
        baseline,
        item_refs(baseline),
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
    )
    records.records[old_key, "plan_repair"] = {
        "schema": "document-plan-v4-repair-v2",
        "base_request_hash": fresh.base_request_hash,
        "candidate": baseline,
        "item_refs": item_refs(baseline),
        "repair_request": old_request,
        "last_response": "old-v2-response-must-not-replay",
        "attempts_used": 1,
        "next_attempt": 1,
    }
    migrated = V3PlanningRepairSession(
        messages, records, {"window_id": "w1"}, "before", limits, [4, 4], context=context
    )
    assert migrated.phase == "routing_repair"
    assert migrated.repair_request["repair_protocol"] == "document-plan-repair-v3"
    assert migrated.legacy_recovery_key == old_key
    assert migrated.next_attempt == migrated.attempts_used == 1
    assert migrated.pending_response is None


@pytest.mark.parametrize(
    ("bad_ranges", "code"),
    [
        ([[0, 1]], "invalid_selection_shape"),
        ([{"from_block": "unknown", "through_block": "unknown"}], "unknown_block_reference"),
    ],
)
def test_v4_generic_patch_reports_recoverable_selector_error(bad_ranges, code):
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    baseline = _candidate(
        _page(subject_ranges=[_whole(resolver, 1, 1)]),
        overview_ranges=[_whole(resolver, 0, 1)],
    )
    refs = item_refs(baseline)
    request = patch_request(
        baseline,
        refs,
        compile_plan_candidate(baseline, context).issues,
        protocol="document-plan-repair-v2",
        target_ranges=[[0, 2]],
    )
    grant = next(
        row
        for row in request["allowed_operations"]
        if row["op"] == "append_item" and row["field"] == "source_only"
    )
    operation = {
        "issue_id": grant["issue_id"],
        "op": grant["op"],
        "item_ref": grant["item_ref"],
        "field": grant["field"],
        "value": {"ranges": bad_ranges, "reason": "Metadata"},
    }
    with pytest.raises(RepairScopeError) as caught:
        apply_plan_patch(
            baseline,
            refs,
            request,
            _patch(request, [operation]),
            block_chars=[4, 4],
            resolver=resolver,
        )
    assert (caught.value.code, caught.value.operation_index) == (code, 0)
    assert caught.value.path.startswith("source_only")
    assert baseline["source_only"] == []
    operation["value"]["ranges"] = [_whole(resolver, 0, 0)]
    corrected = apply_plan_patch(
        baseline,
        refs,
        request,
        _patch(request, [operation]),
        block_chars=[4, 4],
        resolver=resolver,
    )
    assert compile_plan_candidate(corrected.candidate, context).accepted


def test_old_repair_record_is_not_resumed_as_v3():
    records = _Recovery()
    limits = SimpleNamespace(max_attempts=2, input_capacity=100_000)
    first = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4]
    )
    records.save_recovery(
        first.key,
        "plan_repair",
        {
            "schema": "document-plan-v2",
            "base_request_hash": first.base_request_hash,
            "next_attempt": 1,
            "candidate": {"legacy": True},
        },
    )
    resumed = V3PlanningRepairSession(
        _messages(), records, {"window_id": "w1"}, "before", limits, [4]
    )
    assert resumed.next_attempt == 0
    assert resumed.candidate is None


def test_multiple_character_gaps_get_a_stable_progress_fingerprint():
    context = _context(["abcdef"])
    value = _candidate(
        _page(subject_ranges=[{"block_index": 0, "start_char": 2, "end_char": 4}]),
        overview_ranges=[[0, 1]],
    )
    result = compile_plan_candidate(value, context)
    assert len([issue for issue in result.issues if issue.code == "coverage_gap"]) == 2
    records = _Recovery()
    session = V3PlanningRepairSession(
        _messages(),
        records,
        {"window_id": "w1"},
        "before",
        SimpleNamespace(max_attempts=2, input_capacity=100_000),
        [6],
    )
    session.invalid(value, result, 0)
    assert session.fingerprint
    assert len(session.repair_request["issues"]) == 2
