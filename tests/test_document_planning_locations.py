"""Response compatibility must never turn an explicit bad location into all source text."""

import json

import pytest

from openkb.agent.document_planning_response import accept_pages, extract_candidates
from tests.test_document_markdown_planning import _navigation, _parsed


def _accept(text, navigation=None):
    return accept_pages(
        text,
        navigation=_navigation() if navigation is None else navigation,
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )


@pytest.mark.parametrize("shape", ["table", "list", "json"])
@pytest.mark.parametrize("header", ["章节定位", "来源章节", "章节路径"])
def test_location_alias_keeps_good_page_and_rejects_only_unlocated_page(shape, header):
    rows = [
        {"页面名称": "执行流程", "类型": "concept", header: "操作"},
        {"页面名称": "未知流程", "类型": "concept", header: "未提供的章节"},
    ]
    if shape == "table":
        text = f"| 页面名称 | 类型 | {header} |\n|---|---|---|\n" + "\n".join(
            "| " + " | ".join(row.values()) + " |" for row in rows
        )
    elif shape == "list":
        text = "\n".join(
            f"- 页面名称：{row['页面名称']}\n  类型：concept\n  {header}：{row[header]}"
            for row in rows
        )
    else:
        text = json.dumps(rows, ensure_ascii=False)
    result = _accept(text)
    assert [(p.title, p.scope_resolution, p.subject_ranges) for p in result.pages] == [
        ("执行流程", "section", [[1, 2]])
    ]
    assert [item["reason"] for item in result.rejected] == ["unknown_location"]
    assert "未提供的章节" in result.rejected[0]["candidate"]


def test_missing_location_still_allows_fallback_and_optional_columns_are_preserved():
    text = "| 页面名称 | 类型 | 补充信息 |\n|---|---|---|\n| 执行流程 | concept | 简短说明 |"
    rows, _, _ = extract_candidates(text)
    assert rows[0]["补充信息"] == "简短说明"
    result = _accept(text)
    assert not result.rejected
    assert result.pages[0].scope_resolution == "target_fallback"


@pytest.mark.parametrize("description", ["缩略说明", "缩略；说明", "缩略;说明"])
def test_supplied_section_key_is_not_replaced_by_a_descriptive_path(description):
    result = _accept(
        "- 页面名称：执行流程\n  类型：concept\n"
        f"  主体章节：section:run（heading path: {description}）"
    )
    assert not result.rejected
    assert result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize(
    "clue,expected",
    [
        ("准备 > 前提；实施 > 操作", [[0, 1], [1, 2]]),
        ("操作（heading path: 手册 > 实施 > 操作）", [[1, 2]]),
        ("`实施 > 操作`", [[1, 2]]),
    ],
)
def test_exact_compound_paths_resolve_without_fuzzy_matching(clue, expected):
    navigation = _navigation()
    navigation[0]["heading_path"] = ["手册", "准备", "前提"]
    navigation[1]["heading_path"] = ["手册", "实施", "操作"]
    result = _accept(
        f"| 页面名称 | 类型 | 主体章节 |\n|---|---|---|\n| 执行流程 | concept | {clue} |",
        navigation,
    )
    assert not result.rejected
    assert result.pages[0].subject_ranges == expected
    assert result.pages[0].scope_resolution == "section"


@pytest.mark.parametrize(
    "clue,reason",
    [
        ("实施 > 操作", "ambiguous_location"),
        ("前提；不存在", "unknown_location"),
        ("section:run（heading path: 缩略；说明）；不存在", "unknown_location"),
    ],
)
def test_unresolved_compound_path_does_not_accept_partial_or_fallback(clue, reason):
    navigation = _navigation()
    if reason == "ambiguous_location":
        navigation[0]["heading_path"] = ["甲", "实施", "操作"]
        navigation[1]["heading_path"] = ["乙", "实施", "操作"]
    result = _accept(f"- 页面名称：执行流程\n  类型：concept\n  主体章节：{clue}", navigation)
    assert result.pages == []
    assert [item["reason"] for item in result.rejected] == [reason]


@pytest.mark.parametrize("shape", ["json", "list", "table"])
@pytest.mark.parametrize("key_first", [True, False])
def test_section_key_priority_is_independent_of_field_order(shape, key_first):
    fields = [("section_key", "section:run"), ("heading_path", "不存在")]
    if not key_first:
        fields.reverse()
    row = {"name": "Operation", "kind": "concept", **dict(fields)}
    if shape == "json":
        text = json.dumps(row)
    elif shape == "list":
        text = "- " + "\n  ".join(f"{key}: {value}" for key, value in row.items())
    else:
        text = (
            "| " + " | ".join(row) + " |\n|---|---|---|---|\n| " + " | ".join(row.values()) + " |"
        )
    result = _accept(text)
    assert not result.rejected
    assert result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize("shape", ["array", "compact"])
@pytest.mark.parametrize("second", ["section:pre", "不存在"])
def test_all_multiple_selections_are_checked_in_every_shape(shape, second):
    if shape == "array":
        text = json.dumps(
            {"name": "Operation", "kind": "concept", "sections": ["section:run", second]}
        )
    else:
        text = f"- Operation — concept — section:run；{second}"
    result = _accept(text)
    if second == "不存在":
        assert not result.pages
        assert result.rejected[0]["reason"] == "unknown_location"
    else:
        assert not result.rejected
        assert result.pages[0].subject_ranges == [[1, 2], [0, 1]]


def test_heading_path_array_accepts_unique_exact_suffix():
    navigation = _navigation()
    navigation[1]["heading_path"] = ["手册", "实施", "操作"]
    result = _accept(
        json.dumps({"name": "Operation", "kind": "concept", "heading_path": ["实施", "操作"]}),
        navigation,
    )
    assert not result.rejected
    assert result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_selection_in_array_cannot_expand_a_section_to_whole_source(blank):
    result = _accept(
        json.dumps({"name": "Operation", "kind": "concept", "sections": ["section:run", blank]})
    )
    assert not result.pages
    assert result.rejected[0]["reason"] == "unknown_location"
