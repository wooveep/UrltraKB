"""Regressions for numbered native locations and bounded body recovery."""

import copy

import pytest
from pageindex.index.toc_recovery import _literal_entries


def proposal(section, title, page):
    return {"structure": section, "title": title, "physical_index": page}


def test_exact_numbered_location_wins_over_unrelated_unnumbered_titles():
    pages = [
        ("4.14.6.8.7.3\n操作步骤\n", 0),
        ("4.14.6.8.8.3\n操作步骤\n", 0),
        ("### 操作步骤\n", 0),
    ]
    before = copy.deepcopy(pages)
    delivered = proposal("4.14.6.8.7.3", "操作步骤", 575)
    result = _literal_entries([delivered], pages, 574, (574, 0), (577, 0), 4)
    assert result[0]["physical_index"] == 574
    assert result[0]["anchor"]["section"] == delivered["structure"]
    assert pages == before


@pytest.mark.parametrize("prefix", ["### ", ""])
def test_unnumbered_child_is_located_inside_its_native_parent_not_its_wrong_page(prefix):
    pages = [
        (f"4.1 父章节A\n{prefix}功能描述\n正文A\n", 0),
        (f"4.2 父章节B\n{prefix}功能描述\n正文B\n", 0),
    ]
    delivered = [
        proposal("4.1", "父章节A", 1),
        proposal("4.1.1", "功能描述", 2),
        proposal("4.2", "父章节B", 2),
    ]
    result = _literal_entries(delivered, pages, 1, (1, 0), (3, 0), 4)
    assert result[1]["physical_index"] == 1
    assert result[1]["anchor"]["excerpt"] == prefix + "功能描述"
    assert result[1]["structure"] == "4.1.1"


def test_operation_step_numbers_cannot_cut_off_a_native_parent():
    pages = [
        (
            "4.14 异构虚拟化\n4.14.6 VMware资源\n5.在资源池界面查看信息。\n"
            "4.14.6.8 基础操作\n4.14.6.8.7 导出存储池\n4.14.6.8.7.3 操作步骤\n",
            0,
        )
    ]
    delivered = [
        proposal("4.14", "异构虚拟化", 1),
        proposal("4.14.6", "VMware资源", 1),
        proposal("4.14.6.8", "基础操作", 1),
        proposal("4.14.6.8.7", "导出存储池", 1),
        proposal("4.14.6.8.7.3", "操作步骤", 1),
    ]
    result = _literal_entries(delivered, pages, 1, (1, 0), (2, 0), 8)
    assert [n["structure"] for n in result] == [n["structure"] for n in delivered]


@pytest.mark.parametrize("extra_number", ["4.5.1.6.9.1", "1"])
def test_conflicting_original_child_numbers_keep_parent_coverage_and_diagnostics(extra_number):
    text = (
        "4.14.6.11.11 云主机告警信息\n"
        f"4.14.6.11.11.1\n{extra_number} 功能描述\n"
        "功能原文不可丢失。\n4.14.6.11.11.2 前提条件\n条件原文。"
    )
    pages = [(text, 0)]
    delivered = [
        proposal("4.14.6.11.11", "云主机告警信息", 613),
        proposal("4.14.6.11.11.1", "功能描述", 613),
        proposal("4.14.6.11.11.2", "前提条件", 613),
    ]
    result = _literal_entries(delivered, pages, 613, (613, 0), (614, 0), 4)
    assert [x["structure"] for x in result] == ["4.14.6.11.11", "4.14.6.11.11.2"]
    gap = result[0]["navigation_gaps"][0]
    assert gap["section"] == "4.14.6.11.11.1"
    assert gap["reason"] == "conflicting_original_numbers"
    assert extra_number in gap["excerpt"]
    assert pages == [(text, 0)]


def test_original_numbering_restart_uses_active_body_parent_and_preserves_source_numbers():
    from pageindex.index.utils import post_processing

    delivered = [
        proposal(s, title, page)
        for s, title, page in [
            ("4", "功能", 1),
            ("4.5", "旧模块", 2),
            ("4.5.1", "旧子章节", 3),
            ("4.17", "网络", 4),
            ("4.17.10", "负载均衡", 5),
            ("4.17.10.24", "后端监控", 6),
            ("4.5.1.68.1", "请求数", 7),
            ("4.5.1.68.1.1", "功能描述", 7),
            ("4.17.10.25", "下一监控", 8),
            ("5", "附录", 9),
        ]
    ]
    for index, item in enumerate(delivered):
        item["anchor"] = {
            "unit": item["physical_index"],
            "range": [index, index + 1],
            "section": item["structure"],
            "evidence": "text_match",
        }
    tree = post_processing(delivered, 9)

    def flatten(nodes):
        for node in nodes:
            yield node
            yield from flatten(node.get("nodes", []))

    nodes = list(flatten(tree))
    assert [n["start_index"] for n in nodes] == sorted(n["start_index"] for n in nodes)
    stats = next(n for n in nodes if n["structure"] == "4.17.10.24")
    assert stats["nodes"][0]["structure"] == "4.5.1.68.1"
    assert stats["nodes"][0]["hierarchy_note"]["reason"] == "original_number_outside_parent"
    assert stats["nodes"][0]["nodes"][0]["structure"] == "4.5.1.68.1.1"


def test_original_number_diagnostic_cannot_escape_its_body_parent():
    from pageindex.errors import IndexQualityError

    text = "4.1 父章节\n4.2 下一章\n4.1.1\n9 错误标题\n"
    delivered = [proposal("4.1", "父章节", 1), proposal("4.2", "下一章", 1)]
    delivered[0]["navigation_gaps"] = [
        {
            "unit": 1,
            "range": [text.index("4.1.1"), len(text) - 1],
            "excerpt": text[text.index("4.1.1") : -1],
            "section": "4.1.1",
            "title": "错误标题",
            "reason": "conflicting_original_numbers",
        }
    ]
    with pytest.raises(IndexQualityError, match="native parent"):
        _literal_entries(delivered, [(text, 0)], 1, (1, 0), (2, 0), 4)
