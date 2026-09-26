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


@pytest.mark.parametrize("label", ["Page", "page", "Page Name", "页面名称", " **PAGE__NAME** "])
@pytest.mark.parametrize("shape", ["table", "list", "json"])
def test_page_name_labels_share_one_acceptance_rule(label, shape):
    row = {"Section": "操作", label: "Operation", "Kind": "Concept"}
    if shape == "json":
        raw = json.dumps(row)
    elif shape == "list":
        raw = "- " + f"{label}: Operation\n  Kind: Concept\n  Section: 操作"
    else:
        raw = "| " + " | ".join(row) + " |\n|---|---|---|\n| " + " | ".join(row.values()) + " |"
    result = _accept(raw)
    assert not result.rejected
    assert [(page.title, page.subject_ranges) for page in result.pages] == [("Operation", [[1, 2]])]


def test_page_number_does_not_supply_a_page_name():
    result = _accept("| Page Number | Kind | Section |\n|---|---|---|\n| 3 | concept | 操作 |")
    assert not result.pages
    assert result.rejected[0]["reason"] == "missing_title"


@pytest.mark.parametrize("label", ["Summary", "sUMMary", "Overview", "摘要", "概览", "{{SUMMARY}}"])
def test_explicit_summary_category_is_filtered_beside_a_valid_page(label):
    result = _accept(
        json.dumps(
            [
                {"name": "Manual overview", "type": label},
                {"name": "Summary algorithm", "kind": "concept", "section": "操作"},
            ]
        )
    )
    assert not result.rejected
    assert [p.title for p in result.pages] == ["Summary algorithm"]
    assert result.filtered[0]["reason"] == "summary_placeholder"
    assert result.filtered[0]["candidate_key"] in result.resolved_candidates


def test_configured_summary_entity_type_and_valid_group_are_not_filtered():
    result = accept_pages(
        "## 实体\n- Name: Manual\n  Type: summary\n  Section: 操作",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=["summary"],
        existing_targets=set(),
    )
    assert not result.filtered and not result.rejected
    assert result.pages[0].type == "summary"


@pytest.mark.parametrize("label", ["summary algorithm", "摘要方法", "unrecognized-kind"])
def test_summary_filter_does_not_guess_other_categories(label):
    result = _accept(json.dumps({"name": "Manual", "type": label}))
    assert not result.filtered
    assert result.rejected[0]["reason"] == "unknown_kind"


def test_opaque_candidates_use_full_content_and_never_an_empty_name_key():
    prefix = "说明" * 180
    raw = (
        "| 补充说明 | Kind | Section |\n|---|---|---|\n"
        f"| {prefix}甲 | concept | 前提 |\n| {prefix}乙 | concept | 前提 |\n"
        f"| {prefix}甲 | concept | 前提 |"
    )
    result = _accept(raw)
    assert len(result.rejected) == 3
    first, second, duplicate = result.rejected
    assert first["candidate_key"] != second["candidate_key"]
    assert first["candidate_key"] == duplicate["candidate_key"]
    assert all(row["identity_kind"] == "opaque" for row in result.rejected)
    assert all("candidate_name_key" not in row for row in result.rejected)


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_name_aliases_are_rejected_without_column_order_identity(reverse):
    fields = [("Page", "Operation"), ("名称", "Preparation")]
    if reverse:
        fields.reverse()
    result = _accept(json.dumps({**dict(fields), "kind": "concept", "section": "操作"}))
    assert not result.pages
    assert result.rejected[0]["reason"] == "conflicting_field:name"
    assert result.rejected[0]["identity_kind"] == "opaque"
    alternate = _accept(
        json.dumps({**dict(reversed(fields)), "kind": "concept", "section": "操作"})
    )
    assert result.rejected[0]["candidate_key"] == alternate.rejected[0]["candidate_key"]


def test_equivalent_name_aliases_and_separate_title_remain_valid():
    result = _accept(
        json.dumps(
            {
                "Page": "Operation",
                "名称": " Operation ",
                "Page Title": "Operation guide",
                "kind": "concept",
                "section": "操作",
            }
        )
    )
    assert not result.rejected
    assert result.pages[0].name == "concepts/operation"
    assert result.pages[0].title == "Operation guide"


def test_field_label_normalization_does_not_rename_configured_entity_types():
    result = accept_pages(
        '{"Page":"Method","Kind":"entity","Type":"research_method","Section":"操作"}',
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=["research_method", "product"],
        default_entity_type="product",
        existing_targets=set(),
    )
    assert not result.rejected
    assert result.pages[0].type == "research_method"


def test_json_wrapper_type_metadata_does_not_hide_its_pages():
    result = _accept(
        json.dumps(
            {
                "type": "planning",
                "pages": [
                    {"name": "Operation", "kind": "concept", "section": "操作"},
                ],
            }
        )
    )
    assert not result.rejected
    assert result.pages[0].title == "Operation"
