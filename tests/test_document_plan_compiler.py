from copy import deepcopy
from types import SimpleNamespace

import pytest

from openkb.agent.document_page_evidence import page_occurrence_descriptors
from openkb.agent.document_plan import PagePlan
from openkb.agent.document_plan_compiler import PlanningContext, compile_plan_candidate


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
