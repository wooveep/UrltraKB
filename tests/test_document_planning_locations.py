"""Response compatibility must never turn an explicit bad location into all source text."""

import json

import pytest

from openkb.agent.document_planning_response import accept_pages, extract_candidates
from tests.test_document_markdown_planning import _navigation, _parsed


def _accept(text, navigation=None, **kwargs):
    return accept_pages(
        text,
        navigation=_navigation() if navigation is None else navigation,
        **{
            "target": [[0, 2]],
            "parsed": _parsed(),
            "entity_types": [],
            "existing_targets": set(),
            **kwargs,
        },
    )


def test_repeated_new_path_collision_never_authorizes_an_existing_update():
    row = {"name": "Operation", "kind": "concept", "section": "section:run"}
    text = json.dumps(row)
    occupied = {"concepts/operation"}
    first = _accept(text, existing_targets=occupied).pages[0]
    occupied.add(first.name)
    result = _accept(
        text,
        existing_targets=occupied,
        catalog_titles={path: "Unrelated" for path in occupied},
        allowed_update_targets=set(),
    )
    assert not result.rejected and len(result.pages) == 1
    page = result.pages[0]
    assert page.target == "" and page.name not in occupied
    from openkb.agent.document_plan import DocumentPlan, OverviewPlan, validate_plan

    plan = DocumentPlan(
        metadata={"protocol": "document-plan-v3"},
        overview=OverviewPlan(text="Overview"),
        pages=result.pages,
    )
    assert validate_plan(plan, _parsed(), [], occupied)


def test_different_names_with_the_same_slug_get_distinct_paths_and_reuse_own_identity():
    rows = [
        {"name": name, "title": "Shared title", "kind": "concept", "section": "section:run"}
        for name in ("Operation A", "Operation-A")
    ]
    first = _accept(json.dumps(rows))
    assert not first.rejected and len(first.pages) == 2
    assert len({p.name for p in first.pages}) == 2
    again = _accept(json.dumps(rows), accepted=first.pages)
    assert not again.pages and not again.rejected


@pytest.mark.parametrize(
    "clue",
    [
        "标题路径：手册 → 操作",
        "章节路径：手册 > 操作",
        "`手册 / 操作`",
        "section:run；标题路径：简短说明",
        "section:run（标题路径：简短说明）",
        "`section:run`；标题路径：简短说明",
        "`section:run`（标题路径：简短说明）",
    ],
)
def test_location_expressions_share_exact_selection_rules(clue):
    navigation = _navigation()
    navigation[1]["heading_path"] = ["手册", "操作"]
    result = _accept(
        json.dumps({"name": "Operation", "kind": "concept", "section": clue}), navigation
    )
    assert not result.rejected and result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize("path", ["手册 > 操作", "missing path"])
def test_unlabelled_parenthetical_path_is_a_real_selection(path):
    navigation = _navigation()
    navigation[1]["heading_path"] = ["手册", "操作"]
    result = _accept(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "section": f"section:run ({path})",
            }
        ),
        navigation,
    )
    if path == "missing path":
        assert not result.pages and result.rejected[0]["reason"] == "unknown_location"
    else:
        assert not result.rejected and result.pages[0].subject_ranges == [[1, 2]]


@pytest.mark.parametrize(
    "fields",
    [
        {"section_key": "section:run", "heading_path": "前提"},
        {"section": "section:run；标题路径：前提"},
        {"section": "section:run（标题路径：前提）"},
    ],
)
def test_conflicting_path_annotation_is_visible_but_does_not_override_section_key(fields):
    result = _accept(json.dumps({"name": "Operation", "kind": "concept", **fields}))
    assert not result.rejected and result.pages[0].subject_ranges == [[1, 2]]
    assert any("说明不一致" in note for note in result.pages[0].planning_notes)


@pytest.mark.parametrize("selection", [[{"section_key": "section:run"}], ["section:run"]])
def test_structured_selection_retains_shape_beside_labelled_path(selection):
    result = _accept(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "subject_ranges": selection,
                "heading_path": "前提",
            }
        )
    )
    assert not result.rejected and result.pages[0].subject_ranges == [[1, 2]]
    assert any("说明不一致" in note for note in result.pages[0].planning_notes)


@pytest.mark.parametrize("other", ["missing selection", "section:pre"])
def test_unlabelled_other_location_field_remains_a_real_selection(other):
    result = _accept(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "section_key": "section:run",
                "sections": other,
            }
        )
    )
    if other == "section:pre":
        assert not result.rejected and result.pages[0].subject_ranges == [[1, 2], [0, 1]]
    else:
        assert not result.pages and result.rejected[0]["reason"] == "unknown_location"


@pytest.mark.parametrize("label", ["Section key / heading path", "章节键 / 标题路径"])
@pytest.mark.parametrize(
    "clue",
    [
        "section:run; 手册 → 操作说明",
        "section:run 手册 > 操作说明",
        "section:run 前提；section:pre 操作",
        "section:run；section:pre（heading path: 前提）",
    ],
)
def test_combined_field_label_identifies_display_paths_without_hiding_selections(label, clue):
    result = _accept(json.dumps({"name": "Operation", "kind": "concept", label: clue}))
    assert not result.rejected
    expected = [[1, 2], [0, 1]] if "section:pre" in clue else [[1, 2]]
    assert result.pages[0].subject_ranges == expected
    if "section:run 前提" in clue:
        assert any("说明不一致" in note for note in result.pages[0].planning_notes)


@pytest.mark.parametrize(
    "context",
    [
        "section:run；标题路径：前提；unknown clue",
        ["section:run", "标题路径：前提", "unknown clue"],
        [{"section_key": "section:run"}, {"heading_path": "前提"}, "unknown clue"],
    ],
)
def test_context_annotation_retains_key_and_isolates_other_bad_clues(context):
    result = _accept(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "section": "section:pre",
                "context": context,
            }
        )
    )
    assert not result.rejected and result.pages[0].context_ranges == [[1, 2]]
    assert any("说明不一致" in note for note in result.pages[0].planning_notes)
    assert any("未定位必要上下文" in note for note in result.pages[0].planning_notes)


@pytest.mark.parametrize("title", ["输入 → 输出", "操作（标题路径：示例）", "前提；说明"])
def test_literal_title_matching_precedes_location_punctuation(title):
    navigation = _navigation()
    navigation[1]["heading_path"] = [title]
    result = _accept(
        json.dumps({"name": "Operation", "kind": "concept", "section": title}), navigation
    )
    assert result.pages[0].subject_ranges == [[1, 2]] and not result.rejected


@pytest.mark.parametrize(
    "context", ["前提", ["前提"], {"section_key": "section:pre"}, [{"section_key": "section:pre"}]]
)
def test_context_shapes_preserve_equivalent_readable_evidence(context):
    result = _accept(
        json.dumps(
            {"name": "Operation", "kind": "concept", "section": "section:run", "context": context}
        )
    )
    assert not result.rejected and result.pages[0].context_ranges == [[0, 1]]
    from openkb.agent.document_page_evidence import page_occurrence_descriptors
    from tests.test_document_orchestrator import _DummySource

    descriptors = page_occurrence_descriptors(result.pages[0], _DummySource(), _parsed())
    assert {item["block_index"] for item in descriptors} == {0, 1}


@pytest.mark.parametrize(
    "bad",
    [{"block": []}, {"section_key": None}, 17, {"block_index": 99, "start_char": 0, "end_char": 1}],
)
def test_one_bad_context_clue_preserves_the_page_and_other_context(bad):
    result = _accept(
        json.dumps(
            {
                "name": "Operation",
                "kind": "concept",
                "section": "section:run",
                "context": [bad, "前提"],
            }
        ),
        evidence={"blocks": [{"id": "b0", "order": 0}]},
    )
    assert not result.rejected and len(result.pages) == 1
    assert result.pages[0].context_ranges == [[0, 1]]
    assert any("未定位必要上下文" in note for note in result.pages[0].planning_notes)


@pytest.mark.parametrize("later_target", [[[0, 2]], [[0, 1]]])
def test_same_page_merges_context_reference_and_purpose_even_when_body_is_unchanged(later_target):
    row = {"name": "Operation", "kind": "concept", "section": "section:run"}
    first = _accept(json.dumps(row)).pages
    increment = {
        **row,
        "context": "section:pre",
        "references": "《补充手册》尚未导入",
        "purpose": "整理操作前提",
    }
    second = _accept(json.dumps(increment), accepted=first, target=later_target)
    assert not second.rejected and not second.pages
    assert first[0].context_ranges == [[0, 1]]
    assert first[0].purpose == "整理操作前提"
    assert first[0].planning_notes == ["《补充手册》尚未导入"]
    assert second.filtered[0]["reason"] == "merged_page"
    again = _accept(json.dumps(increment), accepted=first, target=later_target)
    assert again.filtered[0]["reason"] == "accepted_echo"
    assert first[0].subject_ranges == [[1, 2]] and first[0].context_ranges == [[0, 1]]
    from openkb.agent.document_page_evidence import page_occurrence_descriptors
    from tests.test_document_orchestrator import _DummySource

    assert {
        r["block_index"] for r in page_occurrence_descriptors(first[0], _DummySource(), _parsed())
    } == {0, 1}


def test_conflicting_increment_preserves_accepted_type_and_information_atomically():
    row = {"name": "Device", "kind": "entity", "type": "product", "section": "section:run"}
    options = {"entity_types": ["product", "person"]}
    first = _accept(json.dumps(row), **options).pages
    before = first[0].to_dict()
    rejected = _accept(
        json.dumps({**row, "type": "person", "context": "section:pre", "references": "changed"}),
        accepted=first,
        **options,
    )
    assert rejected.rejected[0]["reason"] == "accepted_page_conflict"
    assert first[0].to_dict() == before


@pytest.mark.parametrize(
    "purpose", ["整理操作及引用处的注意事项", "外部手册未随本文提供，保留必须参考的要求"]
)
def test_reference_words_in_purpose_do_not_delete_a_supported_page(purpose):
    result = _accept(
        json.dumps(
            {"name": "Operation", "kind": "concept", "section": "section:run", "purpose": purpose}
        )
    )
    assert len(result.pages) == 1 and not result.filtered and not result.rejected
    assert result.pages[0].purpose == purpose


@pytest.mark.parametrize(
    "body",
    [
        "| Name | Kind | Section |\n|---|---|---|\n| Operation | concept | section:run |",
        "- Name: Operation\n  Kind: concept\n  Section: section:run",
    ],
)
@pytest.mark.parametrize(
    "ending", ["\n\nUnfinished note", "\n\nNote: unfinished", "\n\n", "\n## Notes\nUnfinished note"]
)
def test_truncation_preserves_candidates_closed_before_the_tail(body, ending):
    from openkb.agent.document_markdown_planner import _Response

    result = _accept(_Response(body + ending, "length"))
    assert result.truncated and not result.rejected
    assert [page.title for page in result.pages] == ["Operation"]


def test_truncation_keeps_closed_table_row_but_not_partial_last_row():
    from openkb.agent.document_markdown_planner import _Response

    text = (
        "| Name | Kind | Section |\n|---|---|---|\n"
        "| Operation | concept | section:run |\n| Incomplete | concept | sect"
    )
    result = _accept(_Response(text, "length"))
    assert result.truncated and [page.title for page in result.pages] == ["Operation"]


def test_blank_line_after_subsection_heading_preserves_the_candidate():
    result = _accept("### Operation\n\nKind: concept\nSection: section:run")
    assert not result.rejected and result.pages[0].title == "Operation"


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
