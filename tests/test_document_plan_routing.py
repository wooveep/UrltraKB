"""Small source-routing contract tests at the compiler's public seam."""

from copy import deepcopy
from dataclasses import replace

import pytest

from openkb.agent.document_plan_compiler import compile_plan_candidate
from openkb.agent.document_plan_patches import item_refs
from openkb.agent.document_plan_routing import (
    RoutingDecisionError,
    apply_routing_decisions,
    routing_request,
)
from openkb.agent.document_plan_selections import SelectionResolver
from tests.test_document_plan_compiler import _candidate, _context, _page


def test_one_source_item_groups_conflicts_and_preserves_unconflicted_source():
    context = replace(
        _context(["meta", "body1", "body2", "tail"]), selection_protocol="document-plan-v4"
    )
    resolver = SelectionResolver.from_context(context)
    candidate = _candidate(
        _page(subject_ranges=resolver.encode_ranges([[1, 3]])),
        overview_ranges=resolver.encode_ranges([[0, 4]]),
        source_only=[
            {"ranges": resolver.encode_ranges([[0, 1], [1, 2], [2, 3]]), "reason": "mixed"}
        ],
    )
    result = compile_plan_candidate(candidate, context)
    refs = item_refs(candidate)
    request = routing_request(candidate, refs, result.issues, context)
    source = [item for item in request["items"] if item["kind"] == "source_only_conflict"]
    assert len(source) == 1
    assert len(source[0]["diagnostics"]) == 2
    assert len(source[0]["source_pieces"]) == 2
    assert all(
        piece["source"]["text"] in {"body1", "body2"} for piece in source[0]["source_pieces"]
    )
    gap = next(item for item in request["items"] if item["kind"] == "coverage_gap")
    page_ref = refs["sections"]["page_changes"][0]
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": source[0]["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [source[0]["source_pieces"][0]["piece_ref"]],
                "reason": "body1 belongs only in the source",
            },
            {
                "decision_id": gap["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": [page_ref],
            },
        ],
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    assert resolver.decode_ranges(
        routed.candidate["page_changes"][0]["subject_ranges"], "p", target_only=True
    ) == [[2, 4]]
    assert resolver.decode_ranges(
        routed.candidate["source_only"][0]["ranges"], "s", target_only=True
    ) == [[0, 2]]
    assert compile_plan_candidate(routed.candidate, context).issues == ()


def _routing_case():
    context = replace(
        _context(["meta", "body1", "body2", "body3", "tail"]),
        selection_protocol="document-plan-v4",
    )
    resolver = SelectionResolver.from_context(context)
    candidate = _candidate(
        _page(subject_ranges=resolver.encode_ranges([[1, 4]])),
        overview_ranges=resolver.encode_ranges([[0, 5]]),
        source_only=[
            {"ranges": resolver.encode_ranges([[0, 1], [1, 2], [2, 3]]), "reason": "mixed"}
        ],
    )
    refs = item_refs(candidate)
    request = routing_request(
        candidate, refs, compile_plan_candidate(candidate, context).issues, context
    )
    source, gap = request["items"]
    page_ref = refs["sections"]["page_changes"][0]
    return context, resolver, candidate, refs, request, source, gap, page_ref


@pytest.mark.parametrize("retained_count", [0, 1, 2])
def test_source_retention_choices_are_exact(retained_count):
    context, resolver, candidate, refs, request, source, gap, page_ref = _routing_case()
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": source["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [
                    piece["piece_ref"] for piece in source["source_pieces"][:retained_count]
                ],
                "reason": "These pieces remain in the source",
            },
            {
                "decision_id": gap["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": [page_ref],
            },
        ],
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    page = resolver.decode_ranges(
        routed.candidate["page_changes"][0]["subject_ranges"], "page", target_only=True
    )
    assert page == [[1 + retained_count, 5]]
    source_ranges = resolver.decode_ranges(
        routed.candidate["source_only"][0]["ranges"], "source", target_only=True
    )
    assert source_ranges == [[0, 1 + retained_count]]
    assert compile_plan_candidate(routed.candidate, context).issues == ()


@pytest.mark.parametrize(
    "invalid", ["missing", "null", "unknown_piece", "unknown_page", "conflict", "free_range"]
)
def test_invalid_routing_decisions_leave_baseline_untouched(invalid):
    context, _, candidate, refs, request, source, gap, page_ref = _routing_case()
    before = deepcopy(candidate)
    source_decision = {
        "decision_id": source["decision_id"],
        "decision": "route_source_only",
        "retained_pieces": [],
        "reason": "Body owns these pieces",
    }
    gap_decision = {
        "decision_id": gap["decision_id"],
        "decision": "attach_to_pages",
        "page_refs": [page_ref],
    }
    decisions = [source_decision, gap_decision]
    expected = {
        "missing": "repair_decision_missing",
        "null": "repair_piece_out_of_scope",
        "unknown_piece": "repair_piece_out_of_scope",
        "unknown_page": "repair_destination_out_of_scope",
        "conflict": "repair_decision_conflict",
        "free_range": "invalid_selection_shape",
    }[invalid]
    if invalid == "missing":
        decisions.pop()
    elif invalid == "null":
        source_decision["retained_pieces"] = None
    elif invalid == "unknown_piece":
        source_decision["retained_pieces"] = ["piece:unknown"]
    elif invalid == "unknown_page":
        gap_decision["page_refs"] = ["item:unknown"]
    elif invalid == "conflict":
        decisions.append(
            {**source_decision, "retained_pieces": [source["source_pieces"][0]["piece_ref"]]}
        )
    else:
        gap_decision["ranges"] = [[0, 5]]
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": decisions,
    }
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(candidate, refs, request, response, context)
    assert error.value.code == expected
    assert candidate == before


def test_defer_is_pending_and_new_page_uses_only_gap():
    context, resolver, candidate, refs, request, source, gap, _ = _routing_case()
    source_decision = {
        "decision_id": source["decision_id"],
        "decision": "route_source_only",
        "retained_pieces": [],
        "reason": "Page owns these pieces",
    }
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            source_decision,
            {
                "decision_id": gap["decision_id"],
                "decision": "defer",
                "reason": "Need more evidence",
            },
        ],
    }
    with pytest.raises(RoutingDecisionError, match="deferred") as error:
        apply_routing_decisions(candidate, refs, request, response, context)
    assert error.value.code == "repair_deferred"
    response["decisions"][1] = {
        "decision_id": gap["decision_id"],
        "decision": "new_page",
        "kind": "concept",
        "title": "Tail",
        "purpose": "Explain tail",
        "type": None,
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    assert len(routed.candidate["page_changes"]) == 2
    assert resolver.decode_ranges(
        routed.candidate["page_changes"][1]["subject_ranges"], "new", target_only=True
    ) == [[4, 5]]
    assert compile_plan_candidate(routed.candidate, context).issues == ()


def test_gap_can_be_shared_only_with_explicit_authorized_pages():
    context, resolver, candidate, _, _, _, _, _ = _routing_case()
    second = deepcopy(candidate["page_changes"][0])
    second["local_key"] = "c2"
    second["title"] = "Second topic"
    second["subject_ranges"] = resolver.encode_ranges([[3, 4]])
    candidate["page_changes"].append(second)
    refs = item_refs(candidate)
    request = routing_request(
        candidate, refs, compile_plan_candidate(candidate, context).issues, context
    )
    source, gap = request["items"]
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": source["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [],
                "reason": "Body owns the overlap",
            },
            {
                "decision_id": gap["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": refs["sections"]["page_changes"],
            },
        ],
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    assert resolver.decode_ranges(
        routed.candidate["page_changes"][1]["subject_ranges"], "second", target_only=True
    ) == [[3, 5]]
    assert compile_plan_candidate(routed.candidate, context).issues == ()


def test_same_duplicate_normalizes_but_stale_identity_is_rejected():
    context, _, candidate, refs, request, source, gap, page_ref = _routing_case()
    source_decision = {
        "decision_id": source["decision_id"],
        "decision": "route_source_only",
        "retained_pieces": [],
        "reason": "Body owns the overlap",
    }
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            source_decision,
            deepcopy(source_decision),
            {
                "decision_id": gap["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": [page_ref],
            },
        ],
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    assert routed.normalizations[0]["action"] == "deduplicated"
    stale = {**response, "candidate_hash": "stale"}
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(candidate, refs, request, stale, context)
    assert error.value.code == "repair_candidate_mismatch"
    wrong_scope = {**request, "scope_hash": "stale"}
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(candidate, refs, wrong_scope, response, context)
    assert error.value.code == "repair_scope_mismatch"


def test_json_format_marker_is_recorded_without_relaxing_route_scope():
    context, _, candidate, refs, request, source, gap, page_ref = _routing_case()
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": source["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [],
                "reason": "The page retains these pieces",
            },
            {
                "decision_id": gap["decision_id"],
                "decision": "attach_to_pages",
                "page_refs": [page_ref],
            },
        ],
        "type": "json_object",
    }
    routed = apply_routing_decisions(candidate, refs, request, response, context)
    assert routed.normalizations == ({"field": "type", "action": "removed_json_format_marker"},)
    assert compile_plan_candidate(routed.candidate, context).accepted
    for extra in ({"type": "different"}, {"free_range": [[0, 5]]}):
        with pytest.raises(RoutingDecisionError) as error:
            apply_routing_decisions(candidate, refs, request, {**response, **extra}, context)
        assert error.value.code == "invalid_selection_shape"
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(
            candidate, refs, request, {**response, "candidate_hash": "stale"}, context
        )
    assert error.value.code == "repair_candidate_mismatch"


def test_mixed_batch_preserves_only_valid_choices_for_targeted_correction():
    context, _, candidate, refs, request, source, gap, page_ref = _routing_case()
    valid_gap = {
        "decision_id": gap["decision_id"],
        "decision": "attach_to_pages",
        "page_refs": [page_ref],
    }
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": source["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": ["piece:unknown"],
                "reason": "Invalid piece",
            },
            valid_gap,
        ],
    }
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(candidate, refs, request, response, context)
    assert error.value.code == "repair_piece_out_of_scope"
    assert error.value.decision_ids == [source["decision_id"]]
    assert error.value.valid_decisions == {gap["decision_id"]: valid_gap}
    correction = {
        **response,
        "decisions": [
            {
                "decision_id": source["decision_id"],
                "decision": "route_source_only",
                "retained_pieces": [],
                "reason": "Body owns these pieces",
            }
        ],
    }
    routed = apply_routing_decisions(
        candidate,
        refs,
        request,
        correction,
        context,
        preserved=error.value.valid_decisions,
    )
    assert compile_plan_candidate(routed.candidate, context).accepted


def test_heading_only_gap_cannot_create_body_page():
    context = replace(_context(["Heading", "body"]), selection_protocol="document-plan-v4")
    context.parsed.blocks[0].kind = "heading"
    resolver = SelectionResolver.from_context(context)
    candidate = _candidate(
        _page(subject_ranges=resolver.encode_ranges([[1, 2]])),
        overview_ranges=resolver.encode_ranges([[0, 2]]),
    )
    refs = item_refs(candidate)
    request = routing_request(
        candidate, refs, compile_plan_candidate(candidate, context).issues, context
    )
    gap = request["items"][0]
    assert gap["allowed_destinations"]["new_page"] is False
    response = {
        "repair_protocol": "document-plan-repair-v3",
        "candidate_hash": request["candidate_hash"],
        "decisions": [
            {
                "decision_id": gap["decision_id"],
                "decision": "new_page",
                "kind": "concept",
                "title": "Heading",
                "purpose": "Body",
                "type": None,
            }
        ],
    }
    with pytest.raises(RoutingDecisionError) as error:
        apply_routing_decisions(candidate, refs, request, response, context)
    assert error.value.code == "repair_destination_out_of_scope"
