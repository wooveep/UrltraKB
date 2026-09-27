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
    assert "待确认建议" in Path(result.plan.metadata["plan_preview"]).read_text()


@pytest.mark.parametrize("label", ["用途或参考", "用途与说明", "用途及说明"])
def test_notes_mixed_purpose_and_batch_explanation_are_retained_without_extra_pages(label):
    result = accept(
        f"| 标题 | 类别 | {label} | 备注 | 阅读条件 |\n|---|---|---|---|---|\n"
        "| Calibration | 概念 | 描述校准；外部规范未核对 | 暂不建议独立建页 | 窗口外资料未读 |\n\n"
        "以下组织建议仍需要原文核对。"
    )
    assert not result.pages
    assert result.batch_notes[0] == "以下组织建议仍需要原文核对。"
    assert all(
        text in result.batch_notes[1]
        for text in (
            "描述校准；外部规范未核对",
            "暂不建议独立建页",
            "阅读条件：窗口外资料未读",
        )
    )


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


def test_configured_default_only_fills_an_absent_entity_subtype():
    missing = accept('{"title":"Meter","kind":"entity"}', default_entity_type="product")
    assert missing.pages[0].type == "product"
    unknown = accept(
        '{"title":"Meter","kind":"entity","type":"unrecognized"}', default_entity_type="product"
    )
    assert not unknown.pages and unknown.deferred_suggestions[0]["reason"] == "entity_type_unknown"


def test_conflicting_later_labels_survive_deferred_merges_and_prevent_promotion():
    from openkb.agent.document_planning_semantics import record_semantics

    state = {}
    record_semantics(state, accept('{"title":"Setup"}'))
    record_semantics(state, accept('{"title":"Setup","kind":"concept","type":"product"}'))
    result = accept('{"title":"Setup","kind":"concept"}', deferred=state["deferred_suggestions"])
    record_semantics(state, result)
    assert state["deferred_suggestions"][0]["reason"] == "classification_conflict"
    assert state["deferred_suggestions"][0]["labels"]["type"] == "product"
    assert not state["promoted_suggestions"]


def test_deferred_promotion_waits_for_the_whole_response_to_resolve_classification():
    prior = accept('{"title":"Mercury"}')
    result = accept(
        '[{"title":"Mercury","kind":"concept"},{"title":"Mercury","kind":"entity","type":"product"}]',
        deferred=prior.deferred_suggestions,
    )
    assert len(result.pages) == 2 and not result.promoted_suggestions


def test_same_title_with_explicit_different_scopes_retains_both_suggestions():
    result = accept(
        '[{"title":"Installation","kind":"concept","purpose":"Linux server only",'
        '"section":"Linux","context":"Credentials"},{"title":"Installation","kind":"concept",'
        '"purpose":"Windows client only","section":"Windows","context":"Credentials"}]'
    )
    assert len(result.pages) == 2
    assert result.pages[0].key != result.pages[1].key


def test_explicit_extension_accepts_complementary_purposes_with_ordinary_for_wording():
    result = accept(
        '[{"title":"Calibration","kind":"concept",'
        '"purpose":"Procedure for calibrating a meter","section":"Steps"},'
        '{"title":"Calibration prerequisites","kind":"concept","extends":"Calibration",'
        '"purpose":"Prerequisites for calibrating a meter","section":"Preparation"}]'
    )
    assert len(result.pages) == 1 and len(result.pages[0].location_hints) == 2


def test_clear_inline_existing_suggestion_label_extends_without_a_required_field():
    result = accept(
        '[{"title":"Meter (v2)","kind":"entity","type":"product"},'
        '{"title":"Meter (v2)（既有条目，本窗口补充）","kind":"entity","type":"product",'
        '"notes":"补充安装选择"},{"title":"Meter (v3)","kind":"entity","type":"product"}]'
    )
    assert [page.title for page in result.pages] == ["Meter (v2)", "Meter (v3)"]
    assert "补充安装选择" in result.pages[0].planning_notes
