"""Navigation projections never turn headings into unsupplied original evidence."""

import json
from types import SimpleNamespace

import pytest

from openkb.agent.document_navigation_view import navigation_view
from openkb.agent.document_plan_compiler import PlanningContext
from openkb.agent.document_plan_selections import SelectionResolver
from openkb.agent.source_protocol import source_messages


def _data():
    blocks = [SimpleNamespace(id=f"{i + 1:064x}", chars=10) for i in range(4)]
    parsed = SimpleNamespace(blocks=blocks)
    evidence = {
        "group_id": "W",
        "source_id": "s",
        "version_id": "v",
        "parse_id": "p",
        "blocks": [
            {"id": blocks[0].id, "order": 0, "text": "abcdefghij"},
            {"id": blocks[1].id, "order": 1, "text": "abc", "reference": {"start": 0, "end": 3}},
        ],
    }
    navigation = {
        "nodes": [
            {
                "id": "root",
                "parent": None,
                "title": "手册",
                "start": 0,
                "end": 4,
                "summary": "root",
            },
            {
                "id": "n1",
                "parent": "root",
                "title": "准备",
                "start": 0,
                "end": 2,
                "summary": "summary",
            },
            {
                "id": "n2",
                "parent": "root",
                "title": "准备",
                "start": 2,
                "end": 4,
                "summary": "another",
            },
        ]
    }
    return parsed, evidence, navigation


def test_paths_partial_and_outside_are_exact():
    parsed, evidence, navigation = _data()
    rows = navigation_view(navigation, evidence, SimpleNamespace(input_capacity=20_000), parsed)
    assert rows[1]["heading_path"] == ["手册", "准备"]
    assert rows[1]["visibility"] == "partial"
    assert rows[1]["ranges"] == [
        {"from_block": parsed.blocks[0].id, "through_block": parsed.blocks[0].id},
        {"block": parsed.blocks[1].id, "start_char": 0, "end_char": 3},
    ]
    assert rows[2]["visibility"] == "outside_window"
    assert rows[2]["ranges"] == []
    assert rows[1]["section_key"] != rows[2]["section_key"]


@pytest.mark.parametrize(
    "target,expected",
    [
        ([[0, 1]], [[0, 1]]),
        (
            [{"block_index": 1, "start_char": 2, "end_char": 8}],
            [{"block_index": 1, "start_char": 2, "end_char": 3}],
        ),
    ],
)
def test_chapter_selection_intersects_target_and_supplied_character_slices(target, expected):
    from openkb.agent.document_markdown_planner import _navigation_rows
    from openkb.agent.document_planning_response import accept_pages

    parsed, evidence, navigation = _data()
    hints = navigation_view(navigation, evidence, SimpleNamespace(input_capacity=20_000), parsed)
    locator = _navigation_rows(hints, navigation, parsed, target=target, evidence=evidence)
    result = accept_pages(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "section": "section:n1",
                "context": "section:n2",
            }
        ),
        navigation=locator,
        target=target,
        parsed=parsed,
        evidence=evidence,
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.rejected and result.pages[0].subject_ranges == expected
    assert result.pages[0].context_ranges == [[2, 4]]
    outside = accept_pages(
        '{"name":"Outside","kind":"concept","section":"section:n2"}',
        navigation=locator,
        target=target,
        parsed=parsed,
        evidence=evidence,
        entity_types=[],
        existing_targets=set(),
    )
    assert not outside.pages and outside.rejected[0]["reason"] == "subject_outside_target"
    mixed = accept_pages(
        '{"name":"Mixed","kind":"concept","section":"section:n1；section:n2"}',
        navigation=locator,
        target=target,
        parsed=parsed,
        evidence=evidence,
        entity_types=[],
        existing_targets=set(),
    )
    assert not mixed.pages and mixed.rejected[0]["reason"] == "subject_outside_target"
    if target != expected:
        explicit = accept_pages(
            json.dumps({"name": "Explicit", "kind": "concept", "subject_ranges": target}),
            navigation=locator,
            target=target,
            parsed=parsed,
            evidence=evidence,
            entity_types=[],
            existing_targets=set(),
        )
        assert not explicit.pages and explicit.rejected[0]["reason"] == "subject_outside_target"


def test_supplemental_suffix_preserves_window_prefix_and_permissions():
    parsed, evidence, navigation = _data()
    hints = navigation_view(navigation, evidence, SimpleNamespace(input_capacity=20_000), parsed)
    base = source_messages(evidence, {"stage": "planning", "navigation": hints}, "rules")
    extra = {"id": parsed.blocks[2].id, "order": 2, "text": "klmnopqrst"}
    expanded = source_messages(
        evidence,
        {
            "stage": "planning",
            "navigation": hints,
            "supplemental_evidence": {
                "protocol": "document-supplemental-evidence-v1",
                "blocks": [extra],
            },
        },
        "rules",
    )
    first, second = json.loads(base[-1]["content"]), json.loads(expanded[-1]["content"])
    assert base[0] == expanded[0]
    assert first["evidence"] == second["evidence"]
    assert base.identities[parsed.blocks[0].id] == expanded.identities[parsed.blocks[0].id]
    assert expanded.decode_response(
        json.dumps({"block": second["supplemental_evidence"]["blocks"][0]["id"]})
    )
    context = PlanningContext(
        "v",
        "p",
        "W",
        {**evidence, "blocks": [*evidence["blocks"], extra]},
        parsed,
        0,
        2,
        4,
    )
    resolver = SelectionResolver.from_context(context)
    assert resolver.decode(
        {"from_block": parsed.blocks[2].id, "through_block": parsed.blocks[2].id},
        "target",
        target_only=False,
    )
    assert hints[1]["section_key"] not in resolver.by_id


def test_budget_keeps_current_summary_and_counts_removed_navigation():
    parsed, evidence, navigation = _data()
    navigation["nodes"][1]["summary"] = "Current PageIndex summary"
    navigation["nodes"].extend(
        {
            "id": f"far-{index}",
            "parent": "n2",
            "title": f"Far {index}",
            "start": 2,
            "end": 3,
            "summary": "Unrelated distant summary " * 8,
        }
        for index in range(35)
    )
    hints = navigation_view(navigation, evidence, SimpleNamespace(input_capacity=6_000), parsed)
    current = next(row for row in hints if row["section_key"] == "section:n1")
    assert current["summary"] == "Current PageIndex summary"
    assert hints[0]["navigation_projection"]["total_nodes"] == 38
    assert hints[0]["navigation_projection"]["shown_nodes"] < 38
    assert hints[0]["navigation_projection"]["navigation_complete"] is False


def test_navigation_budget_uses_model_tokens_when_available(monkeypatch):
    import litellm

    parsed, evidence, navigation = _data()
    navigation["nodes"][1]["summary"] = "Very long navigation summary " * 100
    calls = []

    def count_tokens(*, model, text):
        calls.append((model, text))
        return 20

    monkeypatch.setattr(litellm, "token_counter", count_tokens)
    hints = navigation_view(
        navigation, evidence, SimpleNamespace(input_capacity=600), parsed, model="example-model"
    )
    assert calls and calls[0][0] == "example-model"
    assert hints[1]["summary"] == navigation["nodes"][1]["summary"]
