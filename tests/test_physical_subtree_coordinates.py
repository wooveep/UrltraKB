"""Pure delivered-verdict validation uses native physical subtree coordinates."""

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index.physical_sections import (
    SectionBoundary,
    finish_title_verification,
    prepare_entries,
    require_corrected_navigation,
)


@pytest.mark.parametrize("repeated_inside", [False, True])
def test_verified_child_membership_uses_native_position_not_printed_page(repeated_inside):
    pages = [("Body continuation.\n", 5) for _ in range(10)]
    pages[0] = ("3.2.4.\nVirtual machines\nIntroduction.\n", 20)
    pages[7] = ("Printed page63\nCPU virtualization\nOriginal CPU explanation.\n", 20)
    if repeated_inside:
        pages[8] = ("Printed page64\ncpu virtualization\nIllustration caption.\n", 20)
    boundary = SectionBoundary("Virtual machines", (65, 0), (75, 0), structure="3.2.4")
    item = {
        "title": "CPU virtualization",
        "structure": "3.2.4.6.1",
        "physical_index": 72,
        "list_index": 0,
    }
    result = finish_title_verification(
        item, {"answer": "yes", "belongs_to_parent": False}, pages, 65, boundary
    )
    assert result["answer"] == "yes" and result["page_number"] == 72
    assert pages[7][0] == "Printed page63\nCPU virtualization\nOriginal CPU explanation.\n"


def test_neighbor_title_on_boundary_page_is_not_part_of_subtree():
    text = "Foreign warning\n1 Parent\nInside child\nOriginal child text.\n"
    entries = [
        {"title": title, "structure": section, "physical_index": 1}
        for title, section in [("Foreign warning", "0.1"), ("Inside child", "1.1")]
    ]
    result = prepare_entries(
        entries, [(text, 30)], boundary=SectionBoundary("Parent", (1, 16), (2, 0), structure="1")
    )
    assert [item["title"] for item in result] == ["Inside child"]


def test_subtree_generation_receives_only_confirmed_parent_text():
    pages = [
        ("Foreign warning\n1 Parent\nInside child\nOriginal child text.\n", 30),
        ("Original final text\n2 Unrelated next chapter\n", 20),
    ]
    before = list(pages)
    boundary = SectionBoundary("Parent", (1, 16), (2, 20), structure="1")
    prompt = "\n".join(
        f"<physical_index_{number}>\n{boundary.page_text(text, number)}"
        for number, (text, _) in enumerate(pages, 1)
    )
    assert "Foreign warning" not in prompt and "Unrelated next chapter" not in prompt
    assert "Inside child" in prompt and "Original final text" in prompt
    assert "<physical_index_1>" in prompt and "<physical_index_2>" in prompt
    assert pages == before


@pytest.mark.parametrize("uncertainty", ["role", "same_page", "cross_boundary", "end_unknown"])
def test_literal_positions_do_not_bypass_role_or_ambiguous_membership(uncertainty):
    pages = [("Body continuation.\n", 5) for _ in range(10)]
    pages[0] = ("3.2.4 Virtual machines\n", 20)
    pages[7] = ("Printed page63\nCPU virtualization\nExplanation.\n", 20)
    start = (65, 0)
    if uncertainty == "same_page":
        pages[7] = (pages[7][0] + "CPU virtualization\nCaption.\n", 30)
    if uncertainty == "cross_boundary":
        prefix = "CPU virtualization\n"
        pages[0] = (prefix + pages[0][0], 30)
        start = (65, len(prefix))
    boundary = SectionBoundary(
        "Virtual machines",
        start,
        (75, 0),
        end_confirmed=uncertainty != "end_unknown",
        structure="3.2.4",
    )
    item = {"title": "CPU virtualization", "physical_index": 72, "list_index": 0}
    verdict = {"answer": "no" if uncertainty == "role" else "yes", "belongs_to_parent": False}
    result = finish_title_verification(item, verdict, pages, 65, boundary)
    assert result["answer"] == "no"
    with pytest.raises(IndexQualityError):
        require_corrected_navigation([result])
