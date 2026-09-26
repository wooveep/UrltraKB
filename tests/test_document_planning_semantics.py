"""Planning preserves supplied classifications before any evidence execution."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_planning_response import accept_pages
from tests.test_document_markdown_planning import _parsed
from tests.test_document_planning_followups import _run_single


def accept(raw, **kwargs):
    return accept_pages(
        raw,
        navigation=[],
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=["person", "product"],
        existing_targets=set(),
        **kwargs,
    )


@pytest.mark.parametrize("label", ["person", "人物", "人员"])
def test_person_aliases_and_entity_groups_keep_the_model_classification(label):
    result = accept(
        "## 建议的实体页面（人物、产品）\n\n"
        f"| 名称 | 类型 | 备注 |\n|---|---|---|\n| Ada | {label} | 仅署名，未核对 |"
    )
    assert not result.rejected
    assert [(page.title, page.kind, page.type) for page in result.pages] == [
        ("Ada", "entity", "person")
    ]
    assert "仅署名，未核对" in result.pages[0].planning_notes


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({}, "classification_unknown"),
        ({"kind": "concept", "type": "person"}, "classification_conflict"),
        ({"kind": "entity"}, "entity_type_unknown"),
    ],
)
def test_uncertain_classification_is_saved_without_inventing_an_executable_page(fields, reason):
    result = accept(json.dumps({"title": "Calibration", **fields}))
    assert result.usable and not result.pages and not result.rejected
    assert len(result.deferred_suggestions) == 1
    pending = result.deferred_suggestions[0]
    assert pending["title"] == "Calibration" and pending["reason"] == reason
    assert "name" not in pending and "target" not in pending


def test_deferred_suggestion_is_persisted_and_does_not_retry_pages(tmp_path, monkeypatch):
    result = _run_single(tmp_path, monkeypatch, ['{"title":"Calibration"}'])
    assert result.plan and not result.plan.pages
    assert len(result.plan.metadata["deferred_suggestions"]) == 1
    report = json.loads(Path(result.report_ref).read_text())
    assert report["suggestions"]["deferred"] == 1
    assert report["planning_execution"]["planning_requests"] == 2
    assert not report["no_pages_recommended"]
    assert "待分类建议" in Path(result.plan.metadata["plan_preview"]).read_text()


def test_notes_mixed_purpose_and_batch_explanation_are_retained_without_extra_pages():
    result = accept(
        "| 标题 | 类别 | 用途或参考 | 备注 | 阅读条件 |\n|---|---|---|---|---|\n"
        "| Calibration | 概念 | 描述校准；外部规范未核对 | 暂不建议独立建页 | 窗口外资料未读 |\n\n"
        "以下组织建议仍需要原文核对。"
    )
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.purpose == "描述校准；外部规范未核对"
    assert {"暂不建议独立建页", "阅读条件：窗口外资料未读"} <= set(page.planning_notes)
    assert result.batch_notes == ["以下组织建议仍需要原文核对。"]
    assert result.annotations[page.key]["labels"] == [{"kind": "概念"}]


def test_display_wrappers_merge_but_versions_remain_distinct():
    result = accept(
        json.dumps(
            [
                {"title": "**Calibration (Linux)**", "kind": "concept"},
                {
                    "title": "`ＣＡＬＩＢＲＡＴＩＯＮ (Linux)`",
                    "kind": "concept",
                    "notes": "第二处依据",
                },
                {"title": "Calibration (Windows)", "kind": "concept"},
            ]
        )
    )
    assert len(result.pages) == 2
    assert "第二处依据" in result.pages[0].planning_notes
    assert len(result.annotations[result.pages[0].key]["origins"]) == 2


def test_missing_category_inherits_only_a_unique_compatible_existing_suggestion():
    prior = accept('{"title":"Calibration","kind":"concept","purpose":"Calibrate the meter"}')
    result = accept('{"title":"Calibration","notes":"补充线索"}', accepted=prior.pages)
    assert not result.pages and not result.deferred_suggestions
    assert "补充线索" in prior.pages[0].planning_notes
    assert result.annotations[prior.pages[0].key]["basis"] == ["inherited"]
    different = accept(
        '{"title":"Calibration","purpose":"Calibrate a different instrument"}', accepted=prior.pages
    )
    assert len(different.deferred_suggestions) == 1
    conflict = accept('{"title":"Calibration","kind":"alien"}', accepted=prior.pages)
    assert len(conflict.deferred_suggestions) == 1


def test_explicit_extension_preserves_identity_and_confirmed_aliases():
    prior = accept('{"title":"Calibration","kind":"concept"}')
    key = prior.pages[0].key
    result = accept(
        '{"title":"Meter setup","extends":"Calibration","kind":"concept","notes":"同一操作的补充"}',
        accepted=prior.pages,
    )
    assert not result.pages and not result.deferred_suggestions
    assert prior.pages[0].key == key
    assert "Meter setup" in result.annotations[key]["aliases"]


def test_deferred_promotion_preserves_both_origins_and_does_not_double_count():
    from openkb.agent.document_planning_semantics import record_semantics

    prior = accept('{"title":"Calibration","notes":"先保留用途"}')
    state = {}
    record_semantics(state, prior)
    result = accept(
        '{"title":"Calibration","kind":"concept","notes":"现在有明确类别"}',
        deferred=state["deferred_suggestions"],
    )
    record_semantics(state, result)
    assert len(result.pages) == 1 and not state["deferred_suggestions"]
    assert len(state["promoted_suggestions"]) == 1
    assert len(state["suggestion_annotations"][result.pages[0].key]["origins"]) == 2
    assert {"先保留用途", "现在有明确类别"} <= set(result.pages[0].planning_notes)
