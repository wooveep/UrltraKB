"""Parent-root identity is a pure source boundary; one rebuild uses genuine Flash."""

from types import SimpleNamespace

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index import page_index
from pageindex.index.physical_sections import (
    SectionBoundary,
    SubtreeQualityError,
    physical_child_entries,
    prepare_entries,
)


def children(parent, pages, entries):
    boundary = SectionBoundary.from_node(parent)
    prepared = prepare_entries(entries, pages, parent["start_index"], boundary)
    selected = physical_child_entries(boundary, prepared, pages, parent["start_index"])
    return page_index.post_processing(selected, parent["end_index"]) if selected else []


def test_subtree_keeps_parent_and_native_two_line_child_without_repair():
    pages = [
        ("Preface.\n1.\nGardening\nOriginal parent text.\n", 20),
        ("1.1.\nPruning\nCut above a bud.\n", 20),
        ("Retain the final page.\n", 10),
    ]
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}
    result = {
        **parent,
        "nodes": children(
            parent,
            pages,
            [
                {"title": "Gardening", "structure": "1", "physical_index": 1},
                {"title": "Pruning", "structure": "1.1", "physical_index": 2},
            ],
        ),
    }
    assert (result["title"], result["start_index"], result["end_index"]) == ("Gardening", 1, 3)
    assert [child["title"] for child in result["nodes"]] == ["Pruning"]
    child = result["nodes"][0]
    assert (child["start_index"], child["end_index"]) == (2, 3)
    assert child["anchor"]["excerpt"] == "1.1.\nPruning" and child["anchor"]["range"] == [0, 12]


@pytest.mark.asyncio
async def test_unchanged_subtree_correction_stops_but_keeps_one_bounded_rebuild(monkeypatch):
    attempts = []

    async def unresolved(*args, **kwargs):
        attempts.append(len(attempts))
        raise IndexQualityError("No verified original heading")

    # Inject failure at the processing boundary, never a model response.
    monkeypatch.setattr(page_index, "_meta_processor_once", unresolved)
    with pytest.raises(SubtreeQualityError) as error:
        await page_index.meta_processor(
            [("An unrelated paragraph.", 20)],
            boundary=SectionBoundary("Parent", (1, 0), (2, 0)),
            opt=SimpleNamespace(),
        )
    assert len(attempts) == 2 and error.value.local_recovery_exhausted is True


def test_subtree_with_only_its_parent_preserves_the_original_leaf():
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}
    pages = [("1.\nGardening\nCare.\n", 20), ("More care.\n", 20), ("Final page.\n", 10)]
    entries = [{"title": "Gardening", "structure": "1", "physical_index": 2}]
    assert children(parent, pages, entries) == []
    assert parent == {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}


@pytest.mark.parametrize(
    "candidate",
    [
        {"physical_index": 2, "structure": "2"},
        {"physical_index": 1, "structure": "2"},
        {"physical_index": 1, "structure": "1", "anchor": {"unit": 1, "range": [25, 38]}},
    ],
)
def test_same_title_with_conflicting_parent_identity_is_not_inherited(candidate):
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 2}
    boundary = SectionBoundary.from_node(parent)
    pages = [("1.\nGardening\nCare.\n", 20), ("2.\nGardening\nAnother chapter.\n", 20)]
    assert not boundary.is_parent_root({"title": "Gardening", "list_index": 0, **candidate}, pages)


@pytest.mark.asyncio
async def test_rebuilt_subtree_can_succeed_after_an_unchanged_failed_correction(
    live_llm, monkeypatch
):
    from pageindex.llm import llm_client_scope

    from ultrakb.index_llm import PageIndexLLM
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    live_llm.install(monkeypatch)
    runtime = PageIndexLLM(
        CompletionExecutor(
            RoleBindings(
                "deepseek/deepseek-flash",
                live_llm.credentials,
                generation={"document_index": {"max_tokens": 512, "reasoning_effort": "none"}},
            ),
            ModelCallPolicy(max_calls=6, retries=0),
        )
    )
    actual_once = page_index._meta_processor_once
    attempts = []

    async def recover(*args, **kwargs):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise IndexQualityError("The first processing attempt has unverified navigation")
        return await actual_once(*args, **kwargs)

    monkeypatch.setattr(page_index, "_meta_processor_once", recover)
    pages = [("1 Gardening\n1.1 Pruning\nCut above a bud.\n", 20)]
    boundary = SectionBoundary("Gardening", (1, 0), (2, 0), structure="1")
    with llm_client_scope(runtime, required=True):
        result = await page_index.meta_processor(
            pages,
            mode="process_no_toc",
            opt=SimpleNamespace(model="deepseek/deepseek-flash"),
            boundary=boundary,
            logger=SimpleNamespace(info=lambda *a: None),
        )
    selected = physical_child_entries(boundary, result, pages)
    assert len(attempts) == 2
    assert any("pruning" in item["title"].lower() for item in selected)
    assert runtime.executor.calls >= 1


def test_same_named_child_is_not_removed_as_the_parent_root():
    parent = {"title": "Gardening", "structure": "1", "start_index": 1, "end_index": 3}
    pages = [("1 Gardening\nCare.\n", 20), ("1.1 Gardening\nPruning.\n", 20), ("End.\n", 10)]
    result = children(
        parent, pages, [{"title": "Gardening", "structure": "1.1", "physical_index": 2}]
    )
    assert [(n["title"], n["start_index"], n["structure"]) for n in result] == [
        ("Gardening", 2, "1.1")
    ]


def test_parent_title_spanning_pages_is_removed_without_losing_its_child():
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
    pages = [("1 Plant\n", 10), ("care\n1.1 Pruning\nCut above a bud.\n", 20), ("End.\n", 10)]
    result = children(
        parent,
        pages,
        [
            {"title": "Plant care", "structure": "1", "physical_index": 1},
            {"title": "Pruning", "structure": "1.1", "physical_index": 2},
        ],
    )
    assert [node["title"] for node in result] == ["Pruning"]


def test_inline_child_number_cannot_be_discarded_when_structure_is_omitted():
    parent = {
        "title": "Gardening",
        "structure": "1",
        "start_index": 1,
        "end_index": 2,
        "anchor": {"unit": 1, "range": [0, 11], "excerpt": "1 Gardening", "section": "1"},
    }
    pages = [("1 Gardening\nIntroduction.\n1.1 Gardening\nPruning.\n", 20), ("End.\n", 10)]
    assert not SectionBoundary.from_node(parent).is_parent_root(
        {"title": "1.1 Gardening", "physical_index": 1}, pages
    )


@pytest.mark.parametrize("parent_title", ["Gardening", "1 Gardening"])
def test_unanchored_parent_cannot_borrow_the_same_named_childs_identity(parent_title):
    parent = {"title": parent_title, "start_index": 1, "end_index": 2}
    candidate = {
        "title": "Gardening",
        "structure": "1.1",
        "physical_index": 1,
        "anchor": {"unit": 1, "range": [19, 32], "section": "1.1", "excerpt": "1.1 Gardening"},
    }
    pages = [("1 Gardening\nIntro.\n1.1 Gardening\nCare.\n", 20), ("End.\n", 10)]
    assert not SectionBoundary.from_node(parent).is_parent_root(candidate, pages)
