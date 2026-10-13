"""Physical title proof and failed-subtree recovery through native meta_processor."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_unique_numbered_heading_moves_anchor_and_requires_role_verification(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    pages = [("See Pruning in chapter 2.\n", 10), ("2 Pruning\nCut above a bud.\n", 10)]
    entries = [{"title": "Pruning", "structure": "2", "physical_index": 1}]
    verified = []

    def generated(*args, **kwargs):
        return entries

    async def verify(**kwargs):
        verified.append(kwargs)
        return '{"answer": "yes"}'

    monkeypatch.setattr(engine, "process_no_toc", generated)
    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.meta_processor(
        pages,
        mode="process_no_toc",
        opt=SimpleNamespace(model="fixture"),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    assert result[0]["physical_index"] == 2
    assert result[0]["anchor"]["excerpt"] == "2 Pruning"
    assert result[0]["anchor"]["evidence"] == "text_match"
    assert result[0]["title_origin"] == "generated"
    assert [call["usage_stage"] for call in verified] == ["verification"]
    assert "<physical_index_2>\n2 Pruning" in verified[0]["prompt"]


@pytest.mark.asyncio
async def test_repeated_header_is_not_a_local_heading_proof(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    pages = [("2 Pruning\nBody on this page.\n", 10)] * 2
    called = []

    async def verify(**kwargs):
        called.append(kwargs)
        return '{"answer": "no"}'

    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.check_title_appearance(
        {"title": "Pruning", "structure": "2", "physical_index": 1, "list_index": 0}, pages
    )
    assert result["answer"] == "no"
    assert len(called) == 1


@pytest.mark.asyncio
async def test_shared_boundary_page_excludes_neighbor_chapter_before_paid_checks(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.index.physical_sections import SectionBoundary

    pages = [("1 Gardening\n1.1 Roses\nCare.\n2 Cooking\n2.1 Sauces\nStir.\n", 20)]
    calls = []

    def generated(*args, **kwargs):
        calls.append("generation")
        return [
            {"title": "Roses", "structure": "1.1", "physical_index": 1},
            {"title": "Sauces", "structure": "2.1", "physical_index": 1},
        ]

    async def verify(**kwargs):
        calls.append("Roses role verification")
        assert "The given section title is Roses." in kwargs["prompt"]
        assert "2 Cooking" not in kwargs["prompt"] and "2.1 Sauces" not in kwargs["prompt"]
        return '{"answer": "yes", "belongs_to_parent": true}'

    monkeypatch.setattr(engine, "process_no_toc", generated)
    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.meta_processor(
        pages,
        mode="process_no_toc",
        opt=SimpleNamespace(model="fixture"),
        boundary=SectionBoundary("Gardening", (1, 0), (1, pages[0][0].index("2 Cooking"))),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    assert [item["title"] for item in result] == ["Roses"]
    assert calls == ["generation", "Roses role verification"]


@pytest.mark.asyncio
async def test_low_accuracy_subtree_repairs_without_rebuilding_parent(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    from pageindex.index.physical_sections import SectionBoundary

    entries = [
        {"title": f"Topic {i}", "physical_index": 20, "structure": str(i + 1)} for i in range(96)
    ]
    calls = []

    def generated(*args, **kwargs):
        calls.append("subtree generation")
        return entries

    async def verify(*args, **kwargs):
        calls.append("96 anchor checks")
        return 45 / 96, [{"list_index": i, "title": f"Topic {i}"} for i in range(45, 96)]

    async def repair(toc, *args, **kwargs):
        calls.append("local correction")
        return toc, []

    monkeypatch.setattr(engine, "process_no_toc", generated)
    monkeypatch.setattr(engine, "verify_toc", verify)
    monkeypatch.setattr(engine, "fix_incorrect_toc_with_retries", repair)
    result = await engine.meta_processor(
        [("Body without unambiguous headings", 100)] * 55,
        mode="process_no_toc",
        start_index=20,
        opt=SimpleNamespace(model="fixture"),
        boundary=SectionBoundary("Parent", (20, 0), (75, 0)),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    assert len(result) == 96
    assert calls == ["subtree generation", "96 anchor checks", "local correction"]


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
async def test_model_verified_heading_without_offset_can_follow_local_anchor(monkeypatch):
    engine = importlib.import_module("pageindex.index.page_index")
    pages = [("Introductory text.\n1 Gardening\nPruning\nCut above a bud.\n", 20)]
    entries = [
        {"title": "Gardening", "structure": "1", "physical_index": 1},
        {"title": "Pruning", "structure": "1.1", "physical_index": 1},
    ]
    verified = []

    async def verify(**kwargs):
        verified.append(kwargs["prompt"])
        return '{"answer": "yes"}'

    monkeypatch.setattr(engine, "process_no_toc", lambda *a, **kw: entries)
    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.meta_processor(
        pages,
        mode="process_no_toc",
        opt=SimpleNamespace(model="fixture"),
        logger=SimpleNamespace(info=lambda *a: None),
    )
    assert [item["title"] for item in result] == ["Gardening", "Pruning"]
    assert result[0]["anchor"]["range"][0] > 0
    assert "anchor" not in result[1]
    assert len(verified) == 2
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
