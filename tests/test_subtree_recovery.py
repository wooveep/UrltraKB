"""Local physical proof, real heading-role checks and bounded subtree recovery."""

import importlib
from types import SimpleNamespace

import pytest

pytest_plugins = ("native_index_fixtures",)


@pytest.mark.asyncio
async def test_unique_numbered_heading_moves_anchor_and_requires_role_verification(
    native_index_llm, live_llm
):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.index.physical_sections import prepare_entries

    pages = [("See Pruning in chapter 2.\n", 10), ("2 Pruning\nCut above a bud.\n", 10)]
    incoming = [{"title": "Pruning", "structure": "2", "physical_index": 1}]
    result = prepare_entries(incoming, pages)
    assert result[0]["physical_index"] == 2
    assert result[0]["anchor"]["excerpt"] == "2 Pruning"
    assert result[0]["anchor"]["evidence"] == "text_match"
    assert result[0]["title_origin"] == "generated"
    result[0]["list_index"] = 0
    verified = await engine.check_title_appearance(result[0], pages)
    assert verified["answer"] == "yes"
    assert native_index_llm.calls == 1
    assert any(
        "<physical_index_2>\n2 Pruning" in message["content"]
        for message in live_llm.calls[-1]["messages"]
    )


def test_repeated_header_is_not_a_local_heading_proof():
    from pageindex.index.physical_sections import prepare_entries, unique_heading

    pages = [("2 Pruning\nBody on this page.\n", 10)] * 2
    proposed = {"title": "Pruning", "structure": "2", "physical_index": 1}
    assert unique_heading(proposed, pages) is None
    [accepted] = prepare_entries([proposed], pages)
    assert "anchor" not in accepted
    assert accepted["title_origin"] == "generated"


@pytest.mark.asyncio
async def test_shared_boundary_page_excludes_neighbor_chapter_before_paid_checks(
    native_index_llm, live_llm
):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.index.physical_sections import SectionBoundary, prepare_entries

    pages = [("1 Gardening\n1.1 Roses\nCare.\n2 Cooking\n2.1 Sauces\nStir.\n", 20)]
    boundary = SectionBoundary("Gardening", (1, 0), (1, pages[0][0].index("2 Cooking")))
    incoming = [
        {"title": "Roses", "structure": "1.1", "physical_index": 1},
        {"title": "Sauces", "structure": "2.1", "physical_index": 1},
    ]
    result = prepare_entries(incoming, pages, boundary=boundary)
    assert [item["title"] for item in result] == ["Roses"]
    result[0]["list_index"] = 0
    verified = await engine.check_title_appearance(result[0], pages, boundary=boundary)
    assert verified["answer"] == "yes"
    prompt = str(live_llm.calls[-1]["messages"])
    assert "The given section title is Roses." in prompt
    assert "2 Cooking" not in prompt and "2.1 Sauces" not in prompt
    assert native_index_llm.calls == 1


def test_low_accuracy_subtree_repairs_without_rebuilding_parent():
    from pageindex.index.physical_sections import SectionBoundary, requires_local_correction

    incorrect = [{"list_index": 1, "title": "Second section"}]
    boundary = SectionBoundary("Parent", (1, 0), (3, 0))
    assert requires_local_correction(0.5, incorrect, boundary=boundary)
    assert not requires_local_correction(0.5, incorrect)
    assert requires_local_correction(0.7, incorrect)
    assert not requires_local_correction(1.0, [], boundary=boundary)


def test_continuation_keeps_prefix_and_cross_page_heading_keeps_both_locations():
    from pageindex.index.physical_sections import merge_continuation, unique_heading

    first = {"title": "Plant care", "physical_index": "<physical_index_1>", "structure": "1"}
    second = {"title": "Plant care", "physical_index": "<physical_index_4>", "structure": "2"}
    assert merge_continuation([first], [dict(first), second]) == [second]
    assert first["structure"] == "1"
    pages = [("Body\n1 Plant\n", 10), ("care\nWater carefully.\n", 10)]
    anchor = unique_heading({"title": "Plant care"}, pages)
    assert anchor["unit"] == 1 and anchor["continuation"]["unit"] == 2
    assert anchor["excerpt"] == "1 Plant"


@pytest.mark.asyncio
async def test_exhausted_subtree_never_falls_back_to_whole_document(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.errors import IndexQualityError
    from pageindex.index.physical_sections import SectionBoundary, SubtreeQualityError

    calls = []

    def failed(*args, **kwargs):
        calls.append(kwargs["start_index"])
        raise IndexQualityError("Wrong subtree anchors")

    monkeypatch.setattr(engine, "process_no_toc", failed)
    with pytest.raises(SubtreeQualityError):
        await engine.meta_processor(
            [("Unclear scan", 10)],
            mode="process_no_toc",
            start_index=7,
            opt=SimpleNamespace(model="fixture", max_subtree_attempts=2),
            boundary=SectionBoundary("A section", (7, 0), (8, 0)),
            logger=SimpleNamespace(info=lambda *a: None),
        )
    assert calls == [7, 7]


@pytest.mark.asyncio
async def test_model_verified_heading_without_offset_can_follow_local_anchor(native_index_llm):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.index.physical_sections import prepare_entries, validate_order

    pages = [("Introductory text.\n1 Gardening\nPruning\nCut above a bud.\n", 20)]
    incoming = [
        {"title": "Gardening", "structure": "1", "physical_index": 1},
        {"title": "Pruning", "structure": "1.1", "physical_index": 1},
    ]
    result = prepare_entries(incoming, pages)
    assert [item["title"] for item in result] == ["Gardening", "Pruning"]
    assert result[0]["anchor"]["range"][0] > 0
    assert "anchor" not in result[1]
    for number, item in enumerate(result):
        item["list_index"] = number
        verified = await engine.check_title_appearance(item, pages)
        assert verified["answer"] == "yes"
    validate_order(result, 1, len(pages))
    assert native_index_llm.calls == 2
    assert all(item["title_origin"] == "generated" for item in result)


@pytest.mark.parametrize("last_page,last_offset", [(1, 10), (0, 40)])
def test_unknown_offset_does_not_hide_a_known_order_violation(last_page, last_offset):
    from pageindex.errors import IndexQualityError
    from pageindex.index.physical_sections import validate_order

    entries = [
        {"physical_index": 1, "anchor": {"range": [30, 35]}},
        {"physical_index": 1},
        {"physical_index": last_page, "anchor": {"range": [last_offset, last_offset + 5]}},
    ]
    with pytest.raises(IndexQualityError):
        validate_order(entries, 0, 2)
