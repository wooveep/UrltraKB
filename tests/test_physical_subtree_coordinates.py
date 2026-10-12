"""Physical subtree evidence must not depend on printed page numbers."""

import json
from types import SimpleNamespace

import pytest
from pageindex.index.page_index import meta_processor
from pageindex.index.physical_sections import SectionBoundary, SubtreeQualityError
from pageindex.llm import IndexResponse, llm_client_scope


class PrintedPageModel:
    def __init__(self, entries=None):
        self.entries = entries

    def complete(self, messages, *, stage):
        if stage == "structure":
            return IndexResponse(
                json.dumps(
                    self.entries
                    or [
                        {
                            "title": "CPU virtualization",
                            "structure": "3.2.4.6.1",
                            "physical_index": "<physical_index_72>",
                        }
                    ]
                )
            )
        # The retained failure recognizes the heading, but incorrectly compares
        # its printed page63 with the parent's physical start65.
        return IndexResponse('{"answer":"yes","belongs_to_parent":false}')

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_inside", [False, True])
async def test_verified_child_membership_uses_native_position_not_printed_page(repeated_inside):
    pages = [("Body continuation.\n", 5) for _ in range(10)]
    pages[0] = ("3.2.4.\nVirtual machines\nIntroduction.\n", 20)
    pages[7] = ("Printed page63\nCPU virtualization\nOriginal CPU explanation.\n", 20)
    if repeated_inside:
        pages[8] = ("Printed page64\ncpu virtualization\nIllustration caption.\n", 20)
    boundary = SectionBoundary("Virtual machines", (65, 0), (75, 0), structure="3.2.4")
    with llm_client_scope(PrintedPageModel(), required=True):
        result = await meta_processor(
            pages,
            mode="process_no_toc",
            start_index=65,
            opt=SimpleNamespace(model="openai/fixture"),
            logger=SimpleNamespace(info=lambda *a: None),
            boundary=boundary,
        )
    assert [(item["title"], item["physical_index"]) for item in result] == [
        ("CPU virtualization", 72)
    ]
    assert pages[7][0] == "Printed page63\nCPU virtualization\nOriginal CPU explanation.\n"


@pytest.mark.asyncio
async def test_neighbor_title_on_boundary_page_is_not_part_of_subtree():
    text = "Foreign warning\n1 Parent\nInside child\nOriginal child text.\n"
    entries = [
        {"title": title, "structure": section, "physical_index": "<physical_index_1>"}
        for title, section in [("Foreign warning", "0.1"), ("Inside child", "1.1")]
    ]
    with llm_client_scope(PrintedPageModel(entries), required=True):
        result = await meta_processor(
            [(text, 30)],
            mode="process_no_toc",
            opt=SimpleNamespace(model="openai/fixture"),
            logger=SimpleNamespace(info=lambda *a: None),
            boundary=SectionBoundary("Parent", (1, 16), (2, 0), structure="1"),
        )
    assert [item["title"] for item in result] == ["Inside child"]


@pytest.mark.asyncio
async def test_subtree_generation_receives_only_confirmed_parent_text():
    class ScopeModel(PrintedPageModel):
        def complete(self, messages, *, stage):
            if stage in {"structure", "verification"}:
                prompt = messages[-1]["content"]
                assert "Foreign warning" not in prompt
                assert "Unrelated next chapter" not in prompt
                assert "Inside child" in prompt
                assert "Original final text" in prompt
                assert "<physical_index_1>" in prompt and "<physical_index_2>" in prompt
                if stage == "verification":
                    assert "current physical page is 1" in prompt
            return super().complete(messages, stage=stage)

    pages = [
        ("Foreign warning\n1 Parent\nInside child\nOriginal child text.\n", 30),
        ("Original final text\n2 Unrelated next chapter\n", 20),
    ]
    before = list(pages)
    runtime = ScopeModel(
        [{"title": "Inside child", "structure": "1.1", "physical_index": "<physical_index_1>"}]
    )
    with llm_client_scope(runtime, required=True):
        result = await meta_processor(
            pages,
            mode="process_no_toc",
            opt=SimpleNamespace(model="openai/fixture"),
            logger=SimpleNamespace(info=lambda *a: None),
            boundary=SectionBoundary("Parent", (1, 16), (2, 20), structure="1"),
        )
    assert [item["title"] for item in result] == ["Inside child"]
    assert pages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertainty", ["role", "same_page", "cross_boundary", "end_unknown"])
async def test_literal_positions_do_not_bypass_role_or_ambiguous_membership(uncertainty):
    class UncertainModel(PrintedPageModel):
        def complete(self, messages, *, stage):
            if stage == "verification":
                return IndexResponse(
                    json.dumps(
                        {
                            "answer": "no" if uncertainty == "role" else "yes",
                            "belongs_to_parent": False,
                        }
                    )
                )
            if stage == "correction":
                return IndexResponse(
                    '{"physical_index":"<physical_index_72>","same_section":false}'
                )
            return super().complete(messages, stage=stage)

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
    with llm_client_scope(UncertainModel(), required=True), pytest.raises(SubtreeQualityError):
        await meta_processor(
            pages,
            mode="process_no_toc",
            start_index=65,
            opt=SimpleNamespace(model="openai/fixture", max_subtree_attempts=1),
            logger=SimpleNamespace(info=lambda *a: None),
            boundary=SectionBoundary(
                "Virtual machines",
                start,
                (75, 0),
                end_confirmed=uncertainty != "end_unknown",
                structure="3.2.4",
            ),
        )
