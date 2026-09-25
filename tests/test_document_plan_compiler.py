from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openkb.agent.document_page_evidence import page_occurrence_descriptors
from openkb.agent.document_plan import PagePlan
from openkb.agent.document_plan_compiler import PlanningContext, compile_plan_candidate
from openkb.agent.document_plan_finalize import finalize_candidate


@pytest.mark.parametrize("section", ["unresolved", "resolutions", "external_references"])
def test_blocked_retry_cannot_change_unrelated_page_relationship(section):
    context = SimpleNamespace(
        retry_page_keys=frozenset({"p1"}),
        open_unresolved=[{"key": "u2", "affected_pages": ["p2"]}],
    )
    delta = {"page_changes": [], "unresolved": [], "resolutions": [],
             "external_references": []}
    delta[section] = [
        {"unresolved_key": "u2"} if section == "resolutions" else
        {"affected_pages": ["p2"]}
    ]

    result = finalize_candidate(
        {}, delta, [], (), "checked", context, [],
    )

    assert result.delta is None
    assert any(issue.code == "retry_page_scope" for issue in result.issues)


def _context(texts, *, target_ranges=None):
    blocks = [
        SimpleNamespace(
            id=f"{index + 1:064x}",
            order=index,
            kind="paragraph",
            text=text,
            chars=len(text),
            location={},
        )
        for index, text in enumerate(texts)
    ]
    parsed = SimpleNamespace(id="c" * 64, blocks=blocks)
    evidence = {
        "group_id": "target-receipt-1",
        "version_id": "b" * 64,
        "parse_id": "c" * 64,
        "blocks": [
            {"id": block.id, "order": block.order, "kind": block.kind, "text": block.text}
            for block in blocks
        ],
    }
    return PlanningContext(
        source_version="b" * 64,
        parse_identity="c" * 64,
        target_receipt_identity="target-receipt-1",
        evidence=evidence,
        parsed=parsed,
        target_start=0,
        target_end=len(blocks),
        total_blocks=len(blocks),
        target_ranges=target_ranges,
    )


def _page(local_key="c1", *, subject_ranges=None, contexts=None, title="中文标题"):
    return {
        "local_key": local_key,
        "target_key": None,
        "target": None,
        "kind": "concept",
        "title": title,
        "purpose": "Explain the selected source material.",
        "subject_ranges": [[0, 1]] if subject_ranges is None else subject_ranges,
        "necessary_context": contexts or [],
    }


def _candidate(page, *, overview_ranges=None, source_only=None, unresolved=None):
    return {
        "overview": {
            "text": "A concise overview.",
            "ranges": overview_ranges or [[0, 1]],
            "limitations": [],
        },
        "page_changes": [page],
        "source_only": source_only or [],
        "unresolved": unresolved or [],
        "resolutions": [],
    }


def test_v5_lossless_singleton_range_and_overview_reason_are_recorded():
    context = replace(_context(["See Guide A."]), selection_protocol="document-plan-v5")
    selection = {"from_block": context.parsed.blocks[0].id,
                 "through_block": context.parsed.blocks[0].id}
    candidate = {
        "overview": {
            "text": "Guide mention.", "ranges": [selection],
            "limitations": [{"ranges": [selection], "reason": "Guide contents are unread."}],
        },
        "page_changes": [{
            "local_key": "guide", "kind": "concept", "title": "Guide", "purpose": "Record mention",
            "subject_ranges": [selection], "necessary_context": [],
        }],
        "source_only": [], "unresolved": [], "resolutions": [],
        "external_references": [{
            "location": selection, "target_document": "Guide A", "target_section": None,
            "affected_pages": ["guide"],
        }],
    }
    result = compile_plan_candidate(candidate, context)
    assert result.accepted, result.issues
    assert result.delta["overview"]["limitations"] == ["Guide contents are unread."]
    assert {row["code"] for row in result.normalizations} == {
        "overview_limitation_reasons", "singleton_selection_list",
    }


def test_v5_reclassifies_only_literal_external_material_with_one_affected_page():
    context = replace(
        _context(["Approval follows 《External Guide》."]),
        selection_protocol="document-plan-v5",
    )
    identity = context.parsed.blocks[0].id
    selected = {"from_block": identity, "through_block": identity}
    candidate = {
        "overview": {"text": "Approval requirement.", "ranges": [selected],
                     "limitations": []},
        "page_changes": [{
            "local_key": "approval", "kind": "concept", "title": "Approval",
            "purpose": "Record required approval", "subject_ranges": [selected],
            "necessary_context": [], "limitations": [],
        }],
        "source_only": [], "resolutions": [], "external_references": [],
        "unresolved": [{
            "location": [selected], "problem_type": "missing_external_material",
            "missing_target": "《External Guide》", "affected_pages": ["approval"],
            "reason": "Its details are not supplied.",
        }],
    }
    accepted = compile_plan_candidate(candidate, context)
    assert accepted.accepted, accepted.issues
    assert accepted.delta["unresolved"] == []
    assert accepted.delta["page_changes"][0]["state"] == "ready"
    assert accepted.delta["external_references"][0]["raw_quote"] == (
        "Approval follows 《External Guide》."
    )
    assert any(row["code"] == "external_reference_reclassified"
               for row in accepted.normalizations)
    candidate["unresolved"][0]["missing_target"] = "《Other Guide》"
    rejected = compile_plan_candidate(candidate, context)
    assert not rejected.accepted
    assert any(issue.code == "invalid_problem_type" for issue in rejected.issues)


def test_v3_compiles_program_owned_identity_state_and_exact_basis_quote():
    context = _context(["正文。", "前置条件。"])
    page = _page(
        contexts=[
            {
                "relation": "applicable_condition",
                "ranges": [[1, 2]],
                "basis_ranges": [[1, 2]],
                "rationale": "The second block supplies the prerequisite.",
            }
        ]
    )
    candidate = _candidate(
        page,
        overview_ranges=[[0, 2]],
        unresolved=[
            {
                "location": [[0, 1]],
                "problem_type": "missing_external_material",
                "missing_target": "The external runbook",
                "affected_pages": ["c1"],
                "reason": "The referenced runbook was not supplied.",
            }
        ],
    )

    first = compile_plan_candidate(candidate, context)
    second = compile_plan_candidate(deepcopy(candidate), context)

    assert first.issues == ()
    assert first.delta is not None
    compiled = first.delta["page_changes"][0]
    assert compiled["title"] == "中文标题"
    assert compiled["name"].startswith("concepts/page-")
    assert compiled["name"] == second.delta["page_changes"][0]["name"]
    assert compiled["target_key"] == "p1"
    assert compiled["state"] == "blocked"
    assert compiled["quality"] == "planned"
    assert compiled["necessary_context"][0]["basis_quote"] == "前置条件。"
    assert compiled["necessary_context"][0]["basis"] == "前置条件。"
    assert (
        first.delta["unresolved"][0]
        | {
            "key": "u1",
            "blocking": True,
            "status": "open",
        }
        == first.delta["unresolved"][0]
    )


@pytest.mark.parametrize(
    ("mutate", "path", "item_ref"),
    [
        (
            lambda value: value["page_changes"][0].update(name="concepts/model-name"),
            "page_changes[0].name",
            "page:c1",
        ),
        (
            lambda value: value["unresolved"][0].update(blocking=False),
            "unresolved[0].blocking",
            "unresolved:0",
        ),
        (
            lambda value: value["page_changes"][0]["necessary_context"][0].update(basis="copy"),
            "page_changes[0].necessary_context[0].basis",
            "context:c1:0",
        ),
        (
            lambda value: value["page_changes"][0]["necessary_context"][0].update(
                basis_quote="copy"
            ),
            "page_changes[0].necessary_context[0].basis_quote",
            "context:c1:0",
        ),
        (
            lambda value: value["page_changes"][0].update(surprise=True),
            "page_changes[0].surprise",
            "page:c1",
        ),
    ],
)
def test_v3_rejects_program_owned_and_unknown_fields(mutate, path, item_ref):
    context = _context(["body", "prerequisite"])
    value = _candidate(
        _page(
            contexts=[
                {
                    "relation": "explicit_reference",
                    "ranges": [[1, 2]],
                    "basis_ranges": [[1, 2]],
                }
            ]
        ),
        overview_ranges=[[0, 2]],
        unresolved=[
            {
                "location": [[0, 1]],
                "problem_type": "missing_prerequisite",
                "missing_target": "details",
                "affected_pages": ["c1"],
                "reason": "details are absent",
            }
        ],
    )
    mutate(value)

    result = compile_plan_candidate(value, context)

    assert result.delta is None
    issue = next(issue for issue in result.issues if issue.path == path)
    assert issue.code in {"program_owned_field", "unknown_field"}
    assert issue.actual is not None
    assert issue.item_ref == item_ref
    assert issue.source_ranges


@pytest.mark.parametrize(
    ("subject_ranges", "code"),
    [
        ([], "range_empty"),
        ([{"block_index": 0, "start_char": 2, "end_char": 9}], "target_range_violation"),
        ([{"block_index": 0, "start_char": 2, "end_char": 5}], "coverage_gap"),
    ],
)
def test_partial_character_target_rejects_empty_outside_and_half_coverage(subject_ranges, code):
    selected = {"block_index": 0, "start_char": 2, "end_char": 8}
    context = _context(["0123456789"], target_ranges=[selected])
    value = _candidate(
        _page(subject_ranges=subject_ranges),
        overview_ranges=[selected],
    )

    result = compile_plan_candidate(value, context)

    assert result.delta is None
    issue = next(issue for issue in result.issues if issue.code == code)
    if code == "coverage_gap":
        assert issue.source_ranges == [{"block_index": 0, "start_char": 5, "end_char": 8}]


@pytest.mark.parametrize(
    "invalid", [[[0, 0]], [{"block_index": 0, "start_char": 0, "end_char": 12}]]
)
def test_invalid_exact_range_never_gets_guessed_into_a_formal_plan(invalid):
    context = _context(["0123456789"])
    result = compile_plan_candidate(_candidate(_page(subject_ranges=invalid)), context)
    assert result.delta is None
    assert any(issue.code in {"range_empty", "range_out_of_bounds"} for issue in result.issues)
    assert not any(issue.code == "coverage_gap" for issue in result.issues)


def test_all_independent_gaps_are_reported_without_expanding_partial_characters():
    context = _context(["abcdef", "ghijkl", "mnopqr"])
    value = _candidate(
        _page(subject_ranges=[{"block_index": 0, "start_char": 2, "end_char": 4}]),
        overview_ranges=[[0, 3]],
    )
    result = compile_plan_candidate(value, context)
    assert result.delta is None
    assert result.unassigned == (
        {"block_index": 0, "start_char": 0, "end_char": 2},
        {"block_index": 0, "start_char": 4, "end_char": 6},
        [1, 3],
    )


def test_coverage_status_distinguishes_pending_unchecked_and_formal_gap():
    context = _context(["prerequisite", "body", "attachment reference"])
    base = _candidate(_page(subject_ranges=[[1, 2]]), overview_ranges=[[0, 3]])

    missing = deepcopy(base)
    del missing["page_changes"][0]["necessary_context"]
    pending = compile_plan_candidate(missing, context)
    assert pending.coverage_status == "provisional"
    assert pending.unassigned == ()
    assert {row.code for row in pending.issues} == {"missing_field", "coverage_pending"}
    assert all(not row.blocking for row in pending.issues if row.code == "coverage_pending")
    assert pending.delta is None
    assert "necessary_context" not in pending.candidate["page_changes"][0]

    malformed = deepcopy(base)
    malformed["page_changes"] = "bad"
    unchecked = compile_plan_candidate(malformed, context)
    assert unchecked.coverage_status == "unchecked"
    assert not any(row.code.startswith("coverage_") for row in unchecked.issues)

    invalid_range = deepcopy(base)
    invalid_range["page_changes"][0]["subject_ranges"] = [[1, 4]]
    unchecked_range = compile_plan_candidate(invalid_range, context)
    assert unchecked_range.coverage_status == "unchecked"
    assert any(row.code == "coverage_pending" for row in unchecked_range.issues)
    assert not any(row.code == "coverage_gap" for row in unchecked_range.issues)

    formal = compile_plan_candidate(base, context)
    assert formal.coverage_status == "checked"
    assert {row.code for row in formal.issues} == {"coverage_gap"}
    assert formal.unassigned == ([0, 1], [2, 3])


def test_bad_coordinate_still_reports_independent_pending_heading():
    context = _context(["heading", "body", "tail"])
    candidate = _candidate(
        _page(subject_ranges=[[1, 3]]),
        overview_ranges=[[0, 3]],
        source_only=[{"ranges": [[261, 262]], "reason": "invalid claim"}],
    )
    result = compile_plan_candidate(candidate, context)
    assert result.coverage_status == "unchecked"
    assert result.unassigned == ()
    assert {row.code for row in result.issues} >= {"range_out_of_bounds", "coverage_pending"}
    assert any(
        row.source_ranges == [[0, 1]] for row in result.issues if row.code == "coverage_pending"
    )


def test_v4_block_selection_compiles_exactly_and_rejects_numeric_aliases():
    context = replace(_context(["heading", "body", "tail"]), selection_protocol="document-plan-v4")
    ids = [block["id"] for block in context.evidence["blocks"]]

    def whole(first, last):
        return {"from_block": ids[first], "through_block": ids[last]}

    candidate = _candidate(
        _page(subject_ranges=[whole(1, 2)]),
        overview_ranges=[whole(0, 2)],
        source_only=[{"ranges": [whole(0, 0)], "reason": "Metadata"}],
    )
    accepted = compile_plan_candidate(candidate, context)
    assert accepted.accepted
    assert accepted.delta["page_changes"][0]["subject_ranges"] == [[1, 3]]
    assert accepted.candidate == candidate

    numeric = deepcopy(candidate)
    numeric["page_changes"][0]["subject_ranges"] = [[1, 3]]
    invalid = compile_plan_candidate(numeric, context)
    assert not invalid.accepted
    assert invalid.coverage_status == "unchecked"
    assert any(row.code == "invalid_selection_shape" for row in invalid.issues)


def test_v4_character_and_sparse_evidence_never_expand_a_slice():
    context = replace(_context(["abcdef", "body", "tail"]), selection_protocol="document-plan-v4")
    ids = [block["id"] for block in context.evidence["blocks"]]
    sliced = _candidate(
        _page(subject_ranges=[{"block": ids[0], "start_char": 1, "end_char": 4}]),
        overview_ranges=[{"block": ids[0], "start_char": 1, "end_char": 4}],
    )
    result = compile_plan_candidate(
        sliced, replace(context, target_ranges=[{"block_index": 0, "start_char": 1, "end_char": 4}])
    )
    assert result.accepted
    assert result.delta["page_changes"][0]["subject_ranges"] == [
        {"block_index": 0, "start_char": 1, "end_char": 4}
    ]
    widened = deepcopy(sliced)
    widened["page_changes"][0]["subject_ranges"][0]["end_char"] = 9
    assert any(
        row.code == "selection_outside_evidence"
        for row in compile_plan_candidate(widened, context).issues
    )

    sparse = replace(
        context,
        evidence={
            **context.evidence,
            "blocks": [context.evidence["blocks"][0], context.evidence["blocks"][2]],
        },
    )
    crossing = _candidate(
        _page(subject_ranges=[{"from_block": ids[0], "through_block": ids[2]}]),
        overview_ranges=[{"from_block": ids[0], "through_block": ids[2]}],
    )
    assert any(
        row.code == "selection_outside_evidence"
        for row in compile_plan_candidate(crossing, sparse).issues
    )


def test_v4_multiple_fragments_of_one_block_use_actual_extents():
    from openkb.agent.document_plan_selections import SelectionError, SelectionResolver

    context = replace(_context(["abcdef"]), selection_protocol="document-plan-v4")
    identity = context.evidence["blocks"][0]["id"]
    fragments = [
        {"id": identity, "order": 0, "text": "ab", "reference": {"start": 0, "end": 2}},
        {"id": identity, "order": 0, "text": "ef", "reference": {"start": 4, "end": 6}},
    ]
    resolver = SelectionResolver.from_context(
        replace(context, evidence={**context.evidence, "blocks": fragments})
    )
    assert resolver.decode(
        {"block": identity, "start_char": 4, "end_char": 6},
        "page_changes[0].subject_ranges[0]",
        target_only=True,
    ) == {"block_index": 0, "start_char": 4, "end_char": 6}
    with pytest.raises(SelectionError) as caught:
        resolver.decode(
            {"from_block": identity, "through_block": identity},
            "page_changes[0].subject_ranges[0]",
            target_only=True,
        )
    assert caught.value.code == "selection_outside_evidence"
    with pytest.raises(SelectionError) as caught:
        resolver.decode(
            {"block": identity, "start_char": 1, "end_char": 5},
            "page_changes[0].subject_ranges[0]",
            target_only=True,
        )
    assert caught.value.code == "selection_outside_evidence"
    full_fragments = [
        fragments[0],
        {"id": identity, "order": 0, "text": "cdef", "reference": {"start": 2, "end": 6}},
    ]
    full = SelectionResolver.from_context(
        replace(context, evidence={**context.evidence, "blocks": full_fragments})
    )
    assert full.decode(
        {"from_block": identity, "through_block": identity},
        "page_changes[0].subject_ranges[0]",
        target_only=True,
    ) == [0, 1]


def test_section_selection_requires_a_complete_supplied_section_inside_its_scope():
    from openkb.agent.document_plan_selections import SelectionError, SelectionResolver

    context = _context(["First", "Second"])
    first, second = [row["id"] for row in context.evidence["blocks"]]
    hints = [
        {
            "section_key": "section:complete",
            "visibility": "complete",
            "ranges": [{"from_block": first, "through_block": second}],
        },
        {
            "section_key": "section:partial",
            "visibility": "partial",
            "ranges": [{"from_block": first, "through_block": first}],
        },
    ]
    resolver = SelectionResolver.from_context(replace(context, navigation_hints=hints))
    assert resolver.decode_ranges(
        [{"section_key": "section:complete"}], "subject_ranges", target_only=True
    ) == [[0, 2]]
    with pytest.raises(SelectionError) as caught:
        resolver.decode_ranges(
            [{"section_key": "section:partial"}], "subject_ranges", target_only=True
        )
    assert caught.value.code == "section_not_complete"
    narrowed = SelectionResolver.from_context(
        replace(context, navigation_hints=hints, target_ranges=[[0, 1]])
    )
    with pytest.raises(SelectionError):
        narrowed.decode_ranges(
            [{"section_key": "section:complete"}], "subject_ranges", target_only=True
        )


def test_character_selection_accepts_only_the_unambiguous_from_block_alias():
    from openkb.agent.document_plan_selections import SelectionError, SelectionResolver

    context = _context(["abcdef"])
    identity = context.evidence["blocks"][0]["id"]
    resolver = SelectionResolver.from_context(context)
    assert resolver.decode(
        {"from_block": identity, "start_char": 1, "end_char": 4},
        "page_changes[0].subject_ranges[0]",
        target_only=True,
    ) == {"block_index": 0, "start_char": 1, "end_char": 4}
    for invalid in (
        {"from_block": identity, "block": identity, "start_char": 1, "end_char": 4},
        {"from_block": identity, "start_char": 4, "end_char": 1},
        {"from_block": identity, "start_char": 1, "end_char": 7},
    ):
        try:
            resolver.decode(invalid, "page_changes[0].subject_ranges[0]", target_only=True)
        except SelectionError:
            pass
        else:
            pytest.fail("Invalid selection was accepted")


def test_v5_compiles_page_limitations_and_external_reference_from_original_text():
    text = "Must follow the external guide."
    context = replace(_context([text]), selection_protocol="document-plan-v5")
    identity = context.evidence["blocks"][0]["id"]
    selected = [{"from_block": identity, "through_block": identity}]
    candidate = _candidate(
        _page(subject_ranges=selected),
        overview_ranges=selected,
    )
    candidate["page_changes"][0]["limitations"] = [
        {"ranges": selected, "reason": "The detailed procedure is in the guide."}
    ]
    candidate["external_references"] = [
        {
            "location": selected,
            "target_document": "external guide",
            "target_section": None,
            "affected_pages": ["c1"],
        }
    ]

    result = compile_plan_candidate(candidate, context)

    assert result.issues == ()
    assert result.delta is not None
    assert result.delta["page_changes"][0]["limitations"] == [
        {
            "ranges": [[0, 1]],
            "reason": "The detailed procedure is in the guide.",
            "source_quote": text,
        }
    ]
    reference = result.delta["external_references"][0]
    assert reference["location"] == [[0, 1]]
    assert reference["raw_quote"] == text
    assert reference["affected_pages"] == ["p1"]
    assert reference["key"].startswith("xref:")


def test_v5_rejects_external_quote_outside_affected_page_evidence():
    context = replace(
        _context(["Page body", "Consult Guide A"]), selection_protocol="document-plan-v5"
    )
    first, second = [row["id"] for row in context.evidence["blocks"]]
    page_range = [{"from_block": first, "through_block": first}]
    reference_range = [{"from_block": second, "through_block": second}]
    candidate = _candidate(
        _page(subject_ranges=page_range), overview_ranges=[*page_range, *reference_range]
    )
    candidate["source_only"] = [{"ranges": reference_range, "reason": "Separate reference"}]
    candidate["external_references"] = [
        {
            "location": reference_range,
            "target_document": "Guide A",
            "target_section": None,
            "affected_pages": ["c1"],
        }
    ]

    result = compile_plan_candidate(candidate, context)

    assert any(issue.code == "reference_outside_page_evidence" for issue in result.issues)
    assert result.delta is None


def test_invalid_context_ranges_with_external_reference_is_reported_not_crashed():
    context = replace(_context(["Follow Guide A."]), selection_protocol="document-plan-v5")
    identity = context.parsed.blocks[0].id
    selected = {"from_block": identity, "through_block": identity}
    candidate = {
        "overview": {"text": "Guide requirement.", "ranges": [selected], "limitations": []},
        "page_changes": [{
            "local_key": "guide", "kind": "concept", "title": "Guide",
            "purpose": "Record the requirement", "subject_ranges": [selected],
            "necessary_context": [{
                "relation": "explicit_reference", "ranges": None,
                "basis_ranges": [selected], "rationale": "The guide is cited.",
            }],
        }],
        "source_only": [], "unresolved": [], "resolutions": [],
        "external_references": [{
            "location": [selected], "target_document": "Guide A",
            "target_section": None, "affected_pages": ["guide"],
        }],
    }

    result = compile_plan_candidate(candidate, context)

    assert not result.accepted
    assert any("necessary_context[0].ranges" in issue.path for issue in result.issues)


def test_compiled_page_evidence_keeps_only_body_and_shared_prerequisite():
    context = _context(["page A body", "page B body", "shared prerequisite", "changelog"])
    value = {
        "overview": {"text": "Two pages.", "ranges": [[0, 4]], "limitations": []},
        "page_changes": [
            _page(
                "a",
                title="Page A",
                subject_ranges=[[0, 1]],
                contexts=[
                    {
                        "relation": "applicable_condition",
                        "ranges": [[2, 3]],
                        "basis_ranges": [[2, 3]],
                        "rationale": "Planner-only explanation.",
                    }
                ],
            ),
            _page("b", title="Page B", subject_ranges=[[1, 2]]),
        ],
        "source_only": [{"ranges": [[3, 4]], "reason": "Historical changelog only."}],
        "unresolved": [],
        "resolutions": [],
    }

    result = compile_plan_candidate(value, context)

    assert result.issues == ()
    change = result.delta["page_changes"][0]
    page = PagePlan(
        key=change["target_key"],
        kind=change["kind"],
        type=change["type"],
        name=change["name"],
        title=change["title"],
        purpose=change["purpose"],
        target=change["target"],
        subject_ranges=change["subject_ranges"],
        necessary_context=change["necessary_context"],
        state=change["state"],
        quality=change["quality"],
        local_key=change["local_key"],
    )
    source = SimpleNamespace(source_id="a" * 32, id="b" * 64)
    descriptors = page_occurrence_descriptors(page, source, context.parsed)

    assert page.state == "ready"
    assert page.quality == "planned"
    assert {row["block_index"] for row in descriptors} == {0, 2}
    assert all("rationale" not in row for row in descriptors)
