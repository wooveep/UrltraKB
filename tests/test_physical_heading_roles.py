"""Literal PDF title locations cannot certify a heading or clip its parent."""

import json

import pytest
from pageindex.index.page_index import check_title_appearance
from pageindex.index.physical_sections import SectionBoundary, prepare_entries
from pageindex.index.utils import post_processing
from pageindex.llm import IndexResponse, llm_client_scope


class RoleModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def acomplete(self, messages, *, stage):
        self.calls.append((stage, messages))
        return IndexResponse(json.dumps({"answer": self.answer}))

    def complete(self, messages, *, stage):
        raise AssertionError("The role check uses the async indexing runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("查看镜像信息，示例：\n# cobbler distro list\n", "cobbler distro list"),
        ("9.3.1 Release\n", "Release"),
        ("2026.10.13 Published\n", "Published"),
    ],
)
async def test_literal_match_does_not_bypass_rejected_heading_role(text, title):
    runtime = RoleModel("no")
    with llm_client_scope(runtime, required=True):
        result = await check_title_appearance(
            {"title": title, "physical_index": 147, "list_index": 0},
            [(text, 10)],
            start_index=147,
            model="openai/fixture",
        )
    assert result["answer"] == "no"
    assert [stage for stage, _ in runtime.calls] == ["verification"]


@pytest.mark.asyncio
async def test_real_heading_keeps_its_location_after_role_verification():
    runtime = RoleModel("yes")
    with llm_client_scope(runtime, required=True):
        result = await check_title_appearance(
            {"title": "系统镜像", "structure": "7.3.8", "physical_index": 147, "list_index": 0},
            [("7.3.8 系统镜像\n查看镜像信息，示例：\n# cobbler distro list\n", 30)],
            start_index=147,
            model="openai/fixture",
        )
    assert result["answer"] == "yes" and result["page_number"] == 147
    assert [stage for stage, _ in runtime.calls] == ["verification"]


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
