"""Native body patterns observed in the retained 1478-page CNware manual."""

import copy

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index.physical_sections import merge_continuation
from pageindex.index.toc_recovery import _literal_entries


def entry(section, title, page=140):
    return {"structure": section, "title": title, "physical_index": page}


@pytest.mark.parametrize(
    ("line", "title", "section"),
    [
        ("4.4.7DRS规则", "DRS 规则", "4.4.7"),
        ("4.4.7DRS规则", "4.4.7DRS规则", "4.4.7"),
        ("4.4.7DRS规\n则", "4.4.7 DRS 规则", "4.4.7"),
        ("4.14.6VMware资源管理", "VMware 资源管理", "4.14.6"),
        ("4.15AI资源管理", "AI 资源管理", "4.15"),
        ("  4.4.7 DRS规则  ", "DRS 规则", "4.4.7"),
    ],
)
def test_mixed_language_native_titles_keep_the_exact_original_span(line, title, section):
    pages = [(line, 0)]
    result = _literal_entries([entry(section, title)], pages, 140, (140, 0), (141, 0), 4)
    anchor = result[0]["anchor"]
    assert anchor["section"] == section
    assert line[slice(*anchor["range"])] == anchor["excerpt"]
    assert anchor["evidence"] == "text_match"


def test_attached_ascii_title_cannot_hide_a_different_literal_section():
    with pytest.raises(IndexQualityError, match="unambiguous body heading"):
        _literal_entries(
            [entry("4.4.8", "4.4.7DRS规则")],
            [("4.4.7DRS规则", 0)],
            140,
            (140, 0),
            (141, 0),
            4,
        )


@pytest.mark.parametrize("incoming_title", ["4.6.12 虚拟机克隆", "4.6.12虚拟机克隆"])
def test_overlapping_source_heading_is_one_entry_despite_model_title_formatting(incoming_title):
    # Physical page 389 has both the parent and a child named 虚拟机克隆.
    pages = [("4.6.12\n虚拟机克隆\n4.6.12.7 虚拟机克隆\n", 0)]
    first = entry("4.6.12", "虚拟机克隆", 389)
    repeat = entry("4.6.12", incoming_title, 389)
    child = entry("4.6.12.7", "虚拟机克隆", 389)
    before = copy.deepcopy([first, repeat, child])
    additional = merge_continuation([first], [repeat, child], page_list=pages, start_index=389)
    assert additional == [child]
    accepted = _literal_entries([first, *additional], pages, 389, (389, 0), (390, 0), 4)
    assert [n["structure"] for n in accepted] == ["4.6.12", "4.6.12.7"]
    assert [first, repeat, child] == before


def test_duplicate_deliveries_resolve_to_the_same_source_before_density_validation():
    pages = [("4.6.12\n虚拟机克隆\n", 0)]
    delivered = [
        entry("4.6.12", "虚拟机克隆", 389),
        entry("4.6.12", "4.6.12 虚拟机克隆", 389),
    ]
    accepted = _literal_entries(delivered, pages, 389, (389, 0), (390, 0), 1)
    assert len(accepted) == 1


def test_same_title_parent_and_child_on_a_boundary_page_remain_distinct():
    # The same pattern occurs on physical page 484 for 回收站.
    pages = [("4.12\n回收站\n4.12.6\n回收站\n", 0)]
    parent = entry("4.12", "回收站", 484)
    child = entry("4.12.6", "回收站", 484)
    additional = merge_continuation(
        [parent, child], [parent, child], page_list=pages, start_index=484
    )
    assert additional == []


def test_only_headings_inside_the_conflict_range_consume_the_node_budget():
    # The retained p474-484 range has 49 source candidates including context;
    # only 43 are inside the replacement interval, which allows 44 nodes.
    # Minimize that pattern to four neighbors followed by one target heading.
    text = "\n".join(f"{i} 标题{i}" for i in range(1, 6))
    pages = [(text, 0)]
    delivered = [entry(str(i), f"标题{i}", 474) for i in range(1, 6)]
    lower = (474, text.index("5 标题5"))
    accepted = _literal_entries(delivered, pages, 474, lower, (475, 0), 4)
    assert [n["structure"] for n in accepted] == ["5"]


def test_a_genuinely_dense_rebuilt_range_still_requires_reduction():
    pages = [("\n".join(f"{i} 标题{i}" for i in range(1, 6)), 0)]
    with pytest.raises(IndexQualityError, match="node density"):
        _literal_entries(
            [entry(str(i), f"标题{i}", 474) for i in range(1, 6)],
            pages,
            474,
            (474, 0),
            (475, 0),
            4,
        )


def test_declared_body_pages_do_not_require_a_full_range_scan_per_heading(monkeypatch):
    from pageindex.index import physical_sections as sections

    pages = [(f"4.6.{i} 标题{i}\n", 0) for i in range(1, 21)]
    delivered = [entry(f"4.6.{i}", f"标题{i}", i) for i in range(1, 21)]
    scanned = []
    locate = sections.heading_candidates

    def measured(item, page_list, start_index=1):
        scanned.append(len(page_list))
        return locate(item, page_list, start_index)

    monkeypatch.setattr(sections, "heading_candidates", measured)
    result = _literal_entries(delivered, pages, 1, (1, 0), (21, 0), 4)
    assert [n["physical_index"] for n in result] == list(range(1, 21))
    assert sum(scanned) <= 2 * len(delivered)


def test_ambiguous_publisher_numbering_cannot_be_reassigned_to_a_different_heading():
    # Physical page 613 contains both of these numbers consecutively.
    text = (
        "4.14.6.11.11 云主机告警信息\n"
        "4.14.6.11.11.1\n4.5.1.6.9.1 功能描述\n"
        "通过该功能可查看云主机告警信息。\n"
    )
    pages = [(text, 0)]
    with pytest.raises(IndexQualityError, match="unambiguous body heading"):
        _literal_entries(
            [entry("4.14.6.11.11.1", "功能描述", 613)],
            pages,
            613,
            (613, 0),
            (614, 0),
            4,
        )
    parent = _literal_entries(
        [entry("4.14.6.11.11", "云主机告警信息", 613)],
        pages,
        613,
        (613, 0),
        (614, 0),
        4,
    )
    assert parent[0]["anchor"]["excerpt"] == "4.14.6.11.11 云主机告警信息"
    assert pages == [(text, 0)]


def test_real_segment_generation_merges_by_the_original_heading_positions(monkeypatch):
    import json

    from pageindex.index import page_index as pi
    from pageindex.index.pipeline import _NullLogger
    from pageindex.llm import IndexResponse, llm_client_scope

    pages = [
        ("4.12\n回收站\n4.12.6\n回收站\n", 20),
        ("4.12.7 模板销毁\n原始正文。", 20),
    ]
    original = copy.deepcopy(pages)
    first = [
        entry("4.12", "回收站", "<physical_index_389>"),
        entry("4.12.6", "回收站", "<physical_index_389>"),
    ]
    second = [
        entry("4.12", "4.12 回收站", "<physical_index_389>"),
        entry("4.12.6", "4.12.6回收站", "<physical_index_389>"),
        entry("4.12.7", "模板销毁", "<physical_index_390>"),
    ]

    class Deliveries:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, stage):
            assert stage == "structure"
            self.calls += 1
            return IndexResponse(json.dumps(first if self.calls == 1 else second))

        async def acomplete(self, messages, *, stage):
            raise AssertionError("This stage uses the injected synchronous boundary")

    # Force two generation groups while exercising the real prompt, transport
    # injection, continuation merge, physical-index conversion and validation.
    monkeypatch.setattr(pi, "page_list_to_group_text", lambda *args: [pages[0][0], pages[1][0]])
    model = Deliveries()
    with llm_client_scope(model, required=True):
        proposed = pi.process_no_toc(pages, start_index=389, model="gpt-4o", logger=_NullLogger())
    result = _literal_entries(proposed, pages, 389, (389, 0), (391, 0), 4)
    assert [n["structure"] for n in result] == ["4.12", "4.12.6", "4.12.7"]
    assert model.calls == 2
    assert pages == original


@pytest.mark.parametrize(
    ("source", "title", "section"),
    [
        ("4.21.18 大屏⾃定义", "大屏自定义", "4.21.18"),
        ("4.21.18\n大屏⾃\n定义", "大屏自定义", "4.21.18"),
        ("说明含ﬁ字形。\n4.6.8 Ofﬁce安装", "Office安装", "4.6.8"),
        ("4.6.8\nOfﬁce\n安装", "Office安装", "4.6.8"),
        ("4.6.8 Ｔｏｏｌｓ安装", "Tools安装", "4.6.8"),
        ("4.6.8 Cafe\u0301配置", "Café配置", "4.6.8"),
    ],
)
def test_pdf_unicode_equivalence_preserves_original_characters_and_offsets(source, title, section):
    pages = [(source, 0)]
    result = _literal_entries([entry(section, title, 1377)], pages, 1377, (1377, 0), (1378, 0), 4)
    anchor = result[0]["anchor"]
    assert source[slice(*anchor["range"])] == anchor["excerpt"]
    assert "说明含ﬁ字形。" not in anchor["excerpt"]
    assert anchor["section"] == section
    assert pages == [(source, 0)]


def test_unicode_equivalence_does_not_permit_a_different_wording():
    with pytest.raises(IndexQualityError, match="unambiguous body heading"):
        _literal_entries(
            [entry("4.21.18", "大屏自动化", 1377)],
            [("4.21.18 大屏⾃定义", 0)],
            1377,
            (1377, 0),
            (1378, 0),
            4,
        )
