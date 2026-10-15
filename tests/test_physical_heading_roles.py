"""Literal PDF title locations cannot certify a heading or clip its parent."""

import pytest
from pageindex.index.physical_sections import (
    SectionBoundary,
    finish_title_verification,
    prepare_entries,
    unique_heading,
)
from pageindex.index.utils import post_processing


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("查看镜像信息，示例：\n# cobbler distro list\n", "cobbler distro list"),
        ("9.3.1 Release\n", "Release"),
        ("2026.10.13 Published\n", "Published"),
    ],
)
def test_literal_match_does_not_bypass_rejected_heading_role(text, title):
    item = {"title": title, "physical_index": 147, "list_index": 0}
    result = finish_title_verification(item, {"answer": "no"}, [(text, 10)], 147)
    assert result["answer"] == "no"
    assert result["page_number"] == 147
    assert item == {"title": title, "physical_index": 147, "list_index": 0}


def test_real_heading_keeps_its_location_after_role_verification():
    item = {"title": "系统镜像", "structure": "7.3.8", "physical_index": 147, "list_index": 0}
    pages = [("7.3.8 系统镜像\n查看镜像信息，示例：\n# cobbler distro list\n", 30)]
    anchor = unique_heading(item, pages, 147)
    result = finish_title_verification(item, {"answer": "yes"}, pages, 147)
    assert result["answer"] == "yes" and result["page_number"] == 147
    assert anchor["unit"] == 147 and anchor["range"] == [0, 10]
    assert anchor["evidence"] == "text_match"


def test_unproved_command_location_cannot_truncate_system_image_parent():
    text = "7.3.8 系统镜像\n查看镜像信息，示例：\n# cobbler distro list\n7.3.9 Snippet\n"
    entries = [
        {"title": title, "structure": section, "physical_index": 147, "appear_start": "no"}
        for title, section in [
            ("系统镜像", "7.3.8"),
            ("cobbler distro list", None),
            ("Snippet", "7.3.9"),
        ]
    ]
    prepared = prepare_entries(entries, [(text, 30)], start_index=147)
    assert all(item.get("title_origin") != "original" for item in prepared)
    parent = post_processing(prepared, 147)[0]
    boundary = SectionBoundary.from_node(parent)
    assert not boundary.start_confirmed and not boundary.end_confirmed
    assert "# cobbler distro list" in boundary.page_text(text, 147)


def test_generated_claim_cannot_certify_its_own_literal_anchor():
    prepared = prepare_entries(
        [
            {
                "title": "cobbler distro list",
                "physical_index": 1,
                "title_origin": "original",
                "anchor": {"unit": 1, "range": [0, 21], "evidence": "source_structure"},
            }
        ],
        [("# cobbler distro list\n", 10)],
    )
    node = post_processing(prepared, 1)[0]
    assert node.get("title_origin") != "original"
    assert not SectionBoundary.from_node(node).start_confirmed
