"""Complete format examples remain parseable and usable with synthetic grants."""

import json
from dataclasses import replace

import pytest

from openkb.agent.document_json_prompts import (
    JSON_RULES,
    PATCH_EXAMPLE,
    PLAN_EXAMPLE,
    REFERENCE_EXAMPLE,
    ROUTING_EXAMPLE,
    repair_task_rules,
)
from openkb.agent.document_plan_compiler import compile_plan_candidate
from openkb.agent.document_plan_patches import apply_plan_patch, item_refs, patch_request
from openkb.agent.document_plan_routing import apply_routing_decisions, routing_request
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.document_protocol import PLAN_RULES
from openkb.agent.document_reference_check import PROTOCOL, validate_reference_decisions
from openkb.agent.document_reference_review import CHECK_RULES
from tests.test_document_plan_compiler import _candidate, _context, _page
from tests.test_document_reference_check import _validator


@pytest.mark.parametrize(
    "mode,example,fields",
    [
        (
            "plan",
            PLAN_EXAMPLE,
            {
                "overview", "page_changes", "source_only", "unresolved", "resolutions",
                "external_references",
            },
        ),
        (
            "syntax_repair",
            PLAN_EXAMPLE,
            {
                "overview", "page_changes", "source_only", "unresolved", "resolutions",
                "external_references",
            },
        ),
        ("field_repair", PATCH_EXAMPLE, {"repair_protocol", "candidate_hash", "operations"}),
        ("routing_repair", ROUTING_EXAMPLE, {"repair_protocol", "candidate_hash", "decisions"}),
        ("reference_check", REFERENCE_EXAMPLE, {"check_protocol", "decisions"}),
    ],
)
def test_each_mode_has_one_complete_json_example(mode, example, fields):
    assert set(json.loads(example)) == fields
    prompt = (
        PLAN_RULES
        if mode == "plan"
        else CHECK_RULES
        if mode == "reference_check"
        else repair_task_rules(mode)
    )
    assert JSON_RULES in prompt
    assert example in prompt


def test_plan_example_compiles_after_binding_its_synthetic_block():
    context = replace(_context(["An example procedure"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    block_id = resolver.encode_ranges([[0, 1]])[0]["from_block"]
    example = json.loads(PLAN_EXAMPLE.replace("@e:examplea", block_id))
    assert compile_plan_candidate(example, context).accepted


def test_patch_example_is_authorized_for_its_synthetic_grant():
    context = replace(_context(["body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    span = resolver.encode_ranges([[0, 1]])[0]
    candidate = _candidate(_page(subject_ranges=[span]), overview_ranges=[span])
    candidate["page_changes"][0]["type"] = "product"
    refs = item_refs(candidate)
    request = patch_request(
        candidate,
        refs,
        compile_plan_candidate(candidate, context).issues,
        protocol="document-plan-repair-v2",
    )
    grant = next(row for row in request["allowed_operations"] if row["op"] == "remove_field")
    example = json.loads(PATCH_EXAMPLE)
    example["candidate_hash"] = request["candidate_hash"]
    example["operations"][0].update(issue_id=grant["issue_id"], item_ref=grant["item_ref"])
    repaired = apply_plan_patch(
        candidate, refs, request, example, block_chars=[4], resolver=resolver
    )
    assert compile_plan_candidate(repaired.candidate, context).accepted


def test_routing_example_is_authorized_for_its_synthetic_grant():
    context = replace(_context(["note", "body"]), selection_protocol="document-plan-v4")
    resolver = SelectionResolver.from_context(context)
    span = resolver.encode_ranges([[0, 2]])[0]
    note = resolver.encode_ranges([[0, 1]])[0]
    candidate = _candidate(
        _page(subject_ranges=[span]),
        overview_ranges=[span],
        source_only=[{"ranges": [note], "reason": "source"}],
    )
    refs = item_refs(candidate)
    request = routing_request(
        candidate, refs, compile_plan_candidate(candidate, context).issues, context
    )
    example = json.loads(ROUTING_EXAMPLE)
    example["candidate_hash"] = request["candidate_hash"]
    example["decisions"][0]["decision_id"] = request["items"][0]["decision_id"]
    routed = apply_routing_decisions(candidate, refs, request, example, context)
    assert compile_plan_candidate(routed.candidate, context).accepted


def test_reference_example_is_valid_for_its_synthetic_pair():
    resolver, candidates, blocks = _validator()
    example = json.loads(REFERENCE_EXAMPLE)
    decision = example["decisions"][0]
    decision.update(reference_key="r1", page_ref="p1")
    decision["target_ranges"] = [{"from_block": blocks[1].id, "through_block": blocks[1].id}]
    result = validate_reference_decisions(
        example,
        candidate_hash="candidate",
        check_input_hash="request",
        pairs=[("r1", "p1")],
        resolver=resolver,
        candidates=candidates,
        required_protocol=PROTOCOL,
    )
    assert not result.issues
