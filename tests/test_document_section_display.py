"""A displayed section label still names one exact, supplied navigation key."""

import json

import pytest

from tests.test_document_planning_locations import _accept


@pytest.mark.parametrize(
    "clue",
    [
        "操作（section:run）",
        "操作 (section:run)",
        "操作（`section:run`）",
        "前提（section:run）",
        "[[section:run]]",
    ],
)
def test_display_title_with_parenthesized_key_resolves_only_that_section(clue):
    result = _accept(json.dumps({"name": "Operation", "kind": "concept", "section": clue}))
    assert result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize(
    "clue",
    [
        "操作（section:invented）",
        "不要选择 section:run，此处仅为说明",
        "操作（section:run）（section:pre）",
        "说明中 [[section:run]] 不是选择",
        "[[section:invented]]、[[section:run]]",
    ],
)
def test_unknown_or_embedded_keys_never_expand_to_a_valid_title_or_whole_source(clue):
    result = _accept(json.dumps({"name": "Operation", "kind": "concept", "section": clue}))
    assert result.pages[0].subject_ranges == []


def test_displayed_key_group_and_explicit_child_span_require_real_compatible_nodes():
    from openkb.agent.document_planning_locations import resolve_hint
    from tests.test_document_orchestrator import _DummyParsed

    parsed = _DummyParsed(4)
    nav = [
        {
            "section_key": "section:nparent",
            "title": "3.3 Parent",
            "heading_path": ["3.3 Parent"],
            "parent": None,
            "original_range": [0, 3],
        },
        {
            "section_key": "section:nfirst",
            "title": "3.3.1 First",
            "heading_path": ["3.3 Parent", "3.3.1 First"],
            "parent": "section:nparent",
            "original_range": [1, 2],
        },
        {
            "section_key": "section:nsecond",
            "title": "3.3.2 Second",
            "heading_path": ["3.3 Parent", "3.3.2 Second"],
            "parent": "section:nparent",
            "original_range": [2, 3],
        },
    ]
    assert resolve_hint("Parent（section:nparent 及其 3.3.1–3.3.2）", nav, parsed)[0] == [[0, 3]]
    assert resolve_hint("Children（section:nfirst, nsecond）", nav, parsed)[0] == [[1, 2], [2, 3]]
    assert resolve_hint("[[section:nfirst]]、[[section:nsecond]]", nav, parsed)[0] == [
        [1, 2],
        [2, 3],
    ]
    for clue in [
        "Parent（section:nmissing 及其 3.3.1–3.3.2）",
        "Children（section:nfirst, nmissing）",
        "Parent（section:nparent 及其 7.1–7.3）",
    ]:
        with pytest.raises(ValueError):
            resolve_hint(clue, nav, parsed)
