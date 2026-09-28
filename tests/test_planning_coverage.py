"""Planning measures actual readable interval unions, not annotation counts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from openkb.agent.document_plan import DocumentPlan, PagePlan, SourceOnlyItem, check_coverage_gaps
from openkb.planning_coverage import planning_coverage, validate_planning_coverage
from openkb.source_coverage import source_coverage


def _parsed(chars=10):
    return SimpleNamespace(
        id="c" * 64,
        blocks=[
            SimpleNamespace(
                id="d" * 64, chars=chars, kind="paragraph", location={}, assets=(), context=""
            )
        ],
        quality=[],
    )


def _page(key, start, end, *, state="ready"):
    return PagePlan(
        key=key,
        kind="concept",
        name=key,
        title=key,
        purpose="Test",
        subject_ranges=[{"block_index": 0, "start_char": start, "end_char": end}],
        state=state,
    )


def test_pending_selection_and_read_receipts_are_separate_unions():
    from openkb.agent.document_page_scope import evidence_scope
    from openkb.agent.document_plan_preview import render_plan_preview
    from openkb.planning_coverage import planning_range_views

    parsed = _parsed()
    plan = DocumentPlan(
        pages=[
            _page("one", 0, 8, state="pending_evidence"),
            _page("two", 4, 10, state="pending_evidence"),
        ]
    )
    initial = planning_range_views(plan, parsed, not_started=True)
    assert initial["selection"]["precise_ratio"] == 1
    assert initial["selection"]["unrouted_ratio"] == 0
    assert initial["reading"]["status"] == "not_started"
    assert initial["reading"]["read_ratio"] is None
    assert planning_coverage(plan, parsed)["executable_page_chars"] == 0
    plan.metadata["planning_ranges"] = initial
    assert "尚未执行" in render_plan_preview(plan)
    for page, span in zip(plan.pages, [(0, 4), (2, 6)]):
        page.state = "ready"
        page.evidence_scope = evidence_scope(
            parsed,
            [],
            [
                {
                    "reference": {
                        "block_id": parsed.blocks[0].id,
                        "start": span[0],
                        "end": span[1],
                    },
                    "routes": [{"route": "page_body"}],
                }
            ],
            [],
            declared=True,
        )
    later = planning_range_views(plan, parsed)
    assert later["reading"]["read_chars"] == 6
    assert later["reading"]["read_ratio"] == 0.6
    assert later["selection"]["precise_ratio"] == 1
    for page in plan.pages:
        del page.evidence_scope["read_ranges"]
    legacy = planning_range_views(plan, parsed)
    assert legacy["reading"]["status"] == "unknown"
    assert legacy["reading"]["read_ratio"] is None


def test_planning_coverage_deduplicates_pages_and_source_only():
    plan = DocumentPlan(
        pages=[_page("one", 0, 4), _page("two", 2, 6), _page("blocked", 7, 10, state="blocked")],
        source_only=[
            SourceOnlyItem(
                ranges=[{"block_index": 0, "start_char": 5, "end_char": 8}], reason="Keep source"
            )
        ],
    )
    coverage = planning_coverage(plan, _parsed())
    validate_planning_coverage(coverage)
    assert (
        coverage["executable_page_chars"],
        coverage["source_only_chars"],
        coverage["missing_chars"],
    ) == (6, 2, 2)
    assert coverage["effective_ratio"] == 0.8
    assert coverage["blocked_pages"] == 1
    assert coverage["missing_ranges"] == [{"block_id": "d" * 64, "start": 8, "end": 10}]


def test_zero_readable_denominator_has_no_percentage():
    coverage = planning_coverage(None, _parsed(0))
    validate_planning_coverage(coverage)
    assert coverage["effective_ratio"] is None


def test_relationship_basis_alone_does_not_count_as_page_content():
    page = _page("one", 0, 2)
    page.necessary_context = [
        {
            "ranges": [{"block_index": 0, "start_char": 2, "end_char": 4}],
            "basis_ranges": [{"block_index": 0, "start_char": 4, "end_char": 8}],
        }
    ]
    coverage = planning_coverage(DocumentPlan(pages=[page]), _parsed())
    assert coverage["executable_page_chars"] == 4
    assert coverage["missing_ranges"] == [{"block_id": "d" * 64, "start": 4, "end": 10}]


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(page_ratio=0.9),
        lambda value: value.update(status="complete"),
        lambda value: value["missing_ranges"][0].update(end=9),
        lambda value: value["missing_ranges"].append(value["missing_ranges"][0].copy()),
        lambda value: value["missing_ranges"][0].update(block_id="invalid"),
        lambda value: value.update(unexpected=True),
    ],
)
def test_persisted_coverage_rejects_inconsistent_projection(change):
    coverage = planning_coverage(DocumentPlan(pages=[_page("one", 0, 6)]), _parsed())
    tampered = deepcopy(coverage)
    change(tampered)
    with pytest.raises(ValueError):
        validate_planning_coverage(tampered)


def test_coverage_gap_reports_only_the_actual_hole():
    plan = DocumentPlan(pages=[_page("left", 0, 2), _page("right", 4, 6)])
    assert check_coverage_gaps(_parsed(6), plan, required_ranges=[[0, 1]]) == [
        {"block_index": 0, "start_char": 2, "end_char": 4}
    ]


def test_published_overlap_prefers_verified_page_over_failed_page():
    source = SimpleNamespace(source_id="a" * 32, id="b" * 64)
    parsed = _parsed()
    reference = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "block_id": parsed.blocks[0].id,
        "start": 0,
        "end": 10,
    }
    report = SimpleNamespace(
        source_occurrences={
            "failed": {
                "reference": reference,
                "route": "page_body",
                "page": "concepts/failed",
                "reason": "planned_page_body",
            },
            "success": {
                "reference": reference,
                "route": "page_body",
                "page": "concepts/success",
                "reason": "planned_page_body",
            },
        },
        published_occurrences={"success"},
        omissions=[],
    )
    coverage = source_coverage(source, parsed, report, published=True)
    assert coverage["ranges"][0]["status"] == "verified"
