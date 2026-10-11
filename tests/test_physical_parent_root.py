"""Native subtree recovery at the injected PageIndex model boundary."""

import json
from types import SimpleNamespace

import pytest
from pageindex.index.page_index import (
    check_title_appearance,
    meta_processor,
    process_large_node_recursively,
)
from pageindex.index.physical_sections import SectionBoundary, SubtreeQualityError
from pageindex.llm import IndexResponse, llm_client_scope


class SubtreeModel:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def complete(self, messages, *, stage):
        self.calls.append((stage, messages))
        if stage == "structure":
            return IndexResponse(json.dumps(self.entries))
        if stage == "verification":
            # A parent is not its own child. Genuine child headings belong.
            parent = "section title is Gardening." in messages[-1]["content"]
            return IndexResponse(json.dumps({"answer": "yes", "belongs_to_parent": not parent}))
        raise AssertionError("Verified parent/child headings need no correction")

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)


@pytest.mark.asyncio
async def test_subtree_keeps_parent_and_native_two_line_child_without_repair():
    pages = [
        ("Preface.\n1.\nGardening\nOriginal parent text.\n", 20),
        ("1.1.\nPruning\nCut above a bud.\n", 20),
        ("Retain the final page.\n", 10),
    ]
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}
    runtime = SubtreeModel(
        [
            {"title": "Gardening", "structure": "1", "physical_index": "<physical_index_1>"},
            {"title": "Pruning", "structure": "1.1", "physical_index": "<physical_index_2>"},
        ]
    )
    with llm_client_scope(runtime, required=True):
        result = await process_large_node_recursively(
            parent,
            pages,
            opt=SimpleNamespace(
                model="openai/fixture", max_page_num_each_node=1, max_token_num_each_node=0
            ),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    assert (result["title"], result["start_index"], result["end_index"]) == (
        "Gardening",
        1,
        3,
    )
    assert [child["title"] for child in result["nodes"]] == ["Pruning"]
    child = result["nodes"][0]
    assert (child["start_index"], child["end_index"]) == (2, 3)
    assert child["anchor"]["excerpt"] == "1.1.\nPruning"
    assert child["anchor"]["range"] == [0, 12]
    assert [stage for stage, _ in runtime.calls] == ["structure", "verification"]


class UnrepairableModel(SubtreeModel):
    def complete(self, messages, *, stage):
        self.calls.append((stage, messages))
        if stage == "structure":
            return IndexResponse(json.dumps(self.entries))
        return IndexResponse(
            '{"answer":"no","belongs_to_parent":false,'
            '"physical_index":"<physical_index_1>","same_section":false}'
        )


@pytest.mark.asyncio
async def test_unchanged_subtree_correction_stops_but_keeps_one_bounded_rebuild():
    runtime = UnrepairableModel(
        [{"title": "Missing", "structure": "1.1", "physical_index": "<physical_index_1>"}]
    )
    with llm_client_scope(runtime, required=True), pytest.raises(SubtreeQualityError):
        await meta_processor(
            [("An unrelated paragraph, without a section heading.", 20)],
            mode="process_no_toc",
            opt=SimpleNamespace(model="openai/fixture"),
            boundary=SectionBoundary("Parent", (1, 0), (2, 0)),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    # Each of two subtree attempts may generate, verify, locate, verify and
    # reconcile once. Repeating the same failed correction adds no evidence.
    assert [stage for stage, _ in runtime.calls] == [
        "structure",
        "verification",
        "correction",
        "verification",
        "correction",
    ] * 2


@pytest.mark.asyncio
async def test_subtree_with_only_its_parent_preserves_the_original_leaf():
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}
    runtime = SubtreeModel(
        [{"title": "Gardening", "structure": "1", "physical_index": "<physical_index_2>"}]
    )
    with llm_client_scope(runtime, required=True):
        result = await process_large_node_recursively(
            dict(parent),
            [("1.\nGardening\nCare.\n", 20), ("More care.\n", 20), ("Final page.\n", 10)],
            opt=SimpleNamespace(
                model="openai/fixture", max_page_num_each_node=1, max_token_num_each_node=0
            ),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    # The generated wrong page may be corrected by unique native evidence.
    assert result == parent
    assert [stage for stage, _ in runtime.calls] == ["structure"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        {"physical_index": 2, "structure": "2"},
        {"physical_index": 1, "structure": "2"},
        {"physical_index": 1, "structure": "1", "anchor": {"unit": 1, "range": [25, 38]}},
    ],
)
async def test_same_title_with_conflicting_parent_identity_is_not_inherited(candidate):
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 2}
    runtime = UnrepairableModel([])
    with llm_client_scope(runtime, required=True):
        result = await check_title_appearance(
            {"title": "Gardening", "list_index": 0, **candidate},
            [("1.\nGardening\nCare.\n", 20), ("2.\nGardening\nAnother chapter.\n", 20)],
            model="openai/fixture",
            boundary=SectionBoundary.from_node(parent),
        )
    assert result["answer"] == "no"
    assert [stage for stage, _ in runtime.calls] == ["verification"]


@pytest.mark.asyncio
async def test_rebuilt_subtree_can_succeed_after_an_unchanged_failed_correction():
    class RebuildingModel(UnrepairableModel):
        def complete(self, messages, *, stage):
            if stage == "structure" and any(s == "structure" for s, _ in self.calls):
                self.entries = [
                    {"title": "Pruning", "structure": "1.1", "physical_index": "<physical_index_1>"}
                ]
            return super().complete(messages, stage=stage)

    runtime = RebuildingModel(
        [{"title": "Missing", "structure": "1.2", "physical_index": "<physical_index_1>"}]
    )
    with llm_client_scope(runtime, required=True):
        result = await meta_processor(
            [("1.1 Pruning\nCut above a bud.\n", 20)],
            mode="process_no_toc",
            opt=SimpleNamespace(model="openai/fixture"),
            boundary=SectionBoundary("Gardening", (1, 0), (2, 0)),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    assert [item["title"] for item in result] == ["Pruning"]
    assert [stage for stage, _ in runtime.calls] == [
        "structure",
        "verification",
        "correction",
        "verification",
        "correction",
        "structure",
    ]


@pytest.mark.asyncio
async def test_same_named_child_is_not_removed_as_the_parent_root():
    class ChildModel(SubtreeModel):
        def complete(self, messages, *, stage):
            if stage == "verification":
                self.calls.append((stage, messages))
                return IndexResponse(
                    '{"answer":"yes","belongs_to_parent":true,"start_begin":"yes"}'
                )
            return super().complete(messages, stage=stage)

    runtime = ChildModel(
        [{"title": "Gardening", "structure": "1.1", "physical_index": "<physical_index_2>"}]
    )
    with llm_client_scope(runtime, required=True):
        result = await process_large_node_recursively(
            {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3},
            [("1 Gardening\nCare.\n", 20), ("1.1 Gardening\nPruning.\n", 20), ("End.\n", 10)],
            opt=SimpleNamespace(
                model="openai/fixture", max_page_num_each_node=1, max_token_num_each_node=0
            ),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    assert [(n["title"], n["start_index"], n["structure"]) for n in result["nodes"]] == [
        ("Gardening", 2, "1.1")
    ]


@pytest.mark.asyncio
async def test_parent_title_spanning_pages_is_removed_without_losing_its_child():
    parent = {
        "title": "Plant care",
        "structure": "1",
        "start_index": 1,
        "end_index": 3,
        "anchor": {
            "unit": 1,
            "range": [0, 8],
            "excerpt": "1 Plant",
            "section": "1",
            "continuation": {"unit": 2, "range": [0, 4], "excerpt": "care"},
        },
    }
    runtime = SubtreeModel(
        [
            {"title": "Plant care", "structure": "1", "physical_index": "<physical_index_1>"},
            {"title": "Pruning", "structure": "1.1", "physical_index": "<physical_index_2>"},
        ]
    )
    with llm_client_scope(runtime, required=True):
        result = await process_large_node_recursively(
            parent,
            [("1 Plant\n", 10), ("care\n1.1 Pruning\nCut above a bud.\n", 20), ("End.\n", 10)],
            opt=SimpleNamespace(
                model="openai/fixture",
                max_page_num_each_node=1,
                max_token_num_each_node=0,
                max_navigation_depth=1,
            ),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    assert [node["title"] for node in result["nodes"]] == ["Pruning"]


@pytest.mark.asyncio
async def test_inline_child_number_cannot_be_discarded_when_structure_is_omitted():
    runtime = UnrepairableModel([])
    parent = {
        "title": "Gardening",
        "structure": "1",
        "start_index": 1,
        "end_index": 2,
        "anchor": {"unit": 1, "range": [0, 11], "excerpt": "1 Gardening", "section": "1"},
    }
    with llm_client_scope(runtime, required=True):
        result = await check_title_appearance(
            {"title": "1.1 Gardening", "physical_index": 1, "list_index": 0},
            [("1 Gardening\nIntroduction.\n1.1 Gardening\nPruning.\n", 20), ("End.\n", 10)],
            model="openai/fixture",
            boundary=SectionBoundary.from_node(parent),
        )
    assert result["answer"] == "no"
    assert [stage for stage, _ in runtime.calls] == ["verification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_title", ["Gardening", "1 Gardening"])
async def test_unanchored_parent_cannot_borrow_the_same_named_childs_identity(parent_title):
    runtime = UnrepairableModel([])
    parent = {"title": parent_title, "start_index": 1, "end_index": 2}
    with llm_client_scope(runtime, required=True):
        result = await check_title_appearance(
            {
                "title": "Gardening",
                "structure": "1.1",
                "physical_index": 1,
                "list_index": 0,
                "anchor": {
                    "unit": 1,
                    "range": [19, 32],
                    "section": "1.1",
                    "excerpt": "1.1 Gardening",
                },
            },
            [("1 Gardening\nIntro.\n1.1 Gardening\nCare.\n", 20), ("End.\n", 10)],
            model="openai/fixture",
            boundary=SectionBoundary.from_node(parent),
        )
    assert result["answer"] == "no"
    assert [stage for stage, _ in runtime.calls] == ["verification"]
