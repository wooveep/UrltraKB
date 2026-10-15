"""Resolve unique original excerpts locally without changing their attribution."""

import copy

import pytest
from pageindex.index.page_parts_policy import PagePartsPolicy


def policy(body="Alpha visible slide", notes="Speaker note only: rotate key every 90 days."):
    return PagePartsPolicy({"unit_count": 1, "page_parts": [{"body": body, "notes": notes}]})


def entry(part, excerpt, span):
    return {
        "structure": "1",
        "title": "Operating rule",
        "title_origin": "generated",
        "physical_index": 1,
        "anchor": {"unit": 1, "part": part, "range": span, "excerpt": excerpt},
    }


@pytest.mark.parametrize(
    "part,excerpt,span,expected",
    [
        ("body", "Alpha visible slide", [0, 18], [0, 19]),
        ("notes", "Speaker note only: rotate key every 90 days.", [0, 42], [0, 44]),
    ],
)
def test_unique_excerpt_resolves_its_original_range(part, excerpt, span, expected):
    source = policy()
    item = entry(part, excerpt, span)
    source.normalize_anchor(item)
    assert source.valid(item)
    assert item["anchor"] == {"unit": 1, "part": part, "range": expected, "excerpt": excerpt}


@pytest.mark.parametrize(
    "changes",
    [
        {"part": "body"},
        {"unit": 2},
        {"excerpt": "invented rule"},
        {"excerpt": ""},
        {"range": [0, "42"]},
    ],
)
def test_recovery_cannot_change_part_unit_or_original_words(changes):
    source = policy()
    item = entry("notes", "Speaker note only: rotate key every 90 days.", [0, 42])
    item["anchor"].update(changes)
    before = copy.deepcopy(item)
    source.normalize_anchor(item)
    assert item == before and not source.valid(item)


def test_ambiguous_excerpt_requires_a_verified_original_range():
    source = policy(body="Repeat. Repeat.")
    item = entry("body", "Repeat.", [0, 5])
    before = copy.deepcopy(item)
    source.normalize_anchor(item)
    assert item == before and not source.valid(item)


def test_generated_media_syntax_cannot_become_an_original_anchor():
    source = PagePartsPolicy(
        {
            "unit_count": 1,
            "page_parts": [{"body": "Text\n![image](media.png)", "notes": ""}],
            "page_generated_spans": [[[5, 24]]],
        }
    )
    item = entry("body", "![image](media.png)", [5, 23])
    before = copy.deepcopy(item)
    source.normalize_anchor(item)
    assert item == before and not source.valid(item)
