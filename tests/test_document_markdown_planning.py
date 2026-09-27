"""Observable Markdown planning acceptance and durable partial handoff."""

import json
from pathlib import Path

import pytest

from openkb.agent.document_markdown_planner import (
    _call,
    _catalog_entries,
    _Response,
    _source_references,
    _state,
)
from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_page_evidence import page_occurrence_descriptors
from openkb.agent.document_plan import DocumentPlan, OverviewPlan, from_dict, to_dict
from openkb.agent.document_planning_report import ordered_fragments
from openkb.agent.document_planning_response import accept_overview, accept_pages
from openkb.agent.document_protocol import plan_messages
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.planning_coverage import planning_coverage, validate_planning_coverage
from openkb.processing import DEFAULT_PROCESSING, InputTooLarge, ProcessingIncomplete, RequestLimits
from tests.test_document_orchestrator import _DummyParsed, _DummySource

SETTINGS = {
    "model": "mock-model",
    "processing": {
        **DEFAULT_PROCESSING,
        "context_tokens": 128_000,
        "max_context_tokens": 128_000,
        "output_tokens": 4_096,
        "max_output_tokens": 4_096,
        "max_attempts": 3,
    },
}


def _parsed():
    parsed = _DummyParsed(2)
    for index, text in enumerate(("前提：须先取得凭据。", "操作：凭据准备后才能执行。")):
        parsed.blocks[index].text = text
        parsed.blocks[index].chars = len(text)
    return parsed


def _navigation():
    return [
        {"section_key": "section:pre", "heading_path": ["前提"], "original_range": [0, 1]},
        {"section_key": "section:run", "heading_path": ["操作"], "original_range": [1, 2]},
    ]


def test_markdown_and_json_pages_keep_good_entries_and_read_context():
    parsed = _parsed()
    markdown = """## 概念页
- 名称：执行流程
  主体章节：操作
  必要上下文：前提
- 名称：位置不明
  主体章节：不存在
"""
    result = accept_pages(
        markdown,
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    assert len(result.pages) == 2
    assert result.pages[0].context_ranges == [[0, 1]]
    assert not result.rejected and result.pages[1].state == "pending_evidence"
    assert result.pages[0].scope_resolution == "section"
    descriptors = page_occurrence_descriptors(result.pages[0], _DummySource(), parsed)
    assert {row["block_index"] for row in descriptors} == {0, 1}

    repaired = accept_pages(
        '{"pages":[{"title":"测试主题","kind":"concept","section":"操作",},]}',
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    assert [page.title for page in repaired.pages] == ["测试主题"]


def test_truncated_json_does_not_accept_a_page_closed_only_by_repair():
    raw = _Response(
        '{"pages":[{"title":"半条","kind":"concept",'
        '"subject_ranges":[{"section_key":"section:run"}]',
        "length",
    )
    result = accept_pages(
        raw,
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert result.pages == []
    assert result.truncated


def test_truncated_json_does_not_promote_nested_context_to_page():
    raw = _Response(
        '{"pages":[{"title":"有效","kind":"concept","section":"section:run"},'
        '{"title":"未完成","kind":"concept","section":"section:run",'
        '"context":{"title":"嵌套标题","kind":"concept","section":"section:pre"}',
        "length",
    )
    result = accept_pages(
        raw,
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert [page.title for page in result.pages] == ["有效"]


def test_unclosed_json_page_is_not_completed_by_repair_after_stop():
    raw = _Response(
        '{"pages":[{"title":"半条","kind":"concept",'
        '"section":"section:run","context":{"title":"嵌套"}',
        "stop",
    )
    result = accept_pages(
        raw,
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.pages
    assert result.truncated


def test_explicit_subject_cannot_claim_another_window():
    result = accept_pages(
        '[{"title":"错位","kind":"concept","subject_ranges":[[0,1]]}]',
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert len(result.pages) == 1 and not result.rejected
    assert result.pages[0].state == "pending_evidence"


def test_empty_explicit_subject_is_rejected_locally():
    result = accept_pages(
        '[{"title":"无范围","kind":"concept","subject_ranges":[]},'
        '{"title":"有效","kind":"concept","section":"操作"}]',
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert [page.title for page in result.pages] == ["无范围", "有效"]
    assert not result.rejected and not result.pages[0].subject_ranges


def test_existing_slug_collision_needs_explicit_supplied_target():
    text = "- 名称：执行流程\n  类别：概念\n  主体章节：操作"
    arguments = dict(
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
    )
    original = accept_pages(text, existing_targets=set(), **arguments).pages[0].name
    created = accept_pages(text, existing_targets={original}, **arguments).pages[0]
    assert created.target == "" and created.name != original
    selected = accept_pages(
        text + f"\n  目标页面：{original}",
        existing_targets={original},
        allowed_update_targets={original},
        **arguments,
    ).pages[0]
    assert selected.target == original


def test_table_keeps_its_own_group_when_later_heading_changes_group():
    result = accept_pages(
        "## 概念页\n| 名称 | 主体章节 |\n|---|---|\n| 操作 | section:run |\n## 实体页",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=["product"],
        existing_targets=set(),
    )
    assert [(page.kind, page.title) for page in result.pages] == [("concept", "操作")]


def test_explanatory_bullet_under_category_is_not_a_page():
    result = accept_pages(
        "## 概念页\n- 以下页面均依据已读原文。\n- 名称：操作\n  主体章节：操作",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert [page.title for page in result.pages] == ["操作"]


def test_table_type_conflicting_with_category_is_rejected():
    result = accept_pages(
        "## 概念页\n| 名称 | 类型 | 主体章节 |\n|---|---|---|\n| 人物页 | person | section:run |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=["person"],
        existing_targets=set(),
    )
    assert not result.pages and not result.rejected
    assert result.deferred_suggestions[0]["reason"] == "classification_conflict"


def test_ambiguous_json_is_not_an_overview():
    assert accept_overview('{"overview":"甲","summary":"乙"}').reason == "overview_unusable"
    assert accept_overview('{"foo":1}').text == ""


def test_recovery_state_rejects_missing_shape_but_accepts_empty_windows():
    class Saved:
        value = None

        def load_recovery(self, *_args):
            return self.value

    saved = Saved()
    saved.value = {"protocol": "document-planning-markdown-v1"}
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
        _state(saved, "unused", [], True)
    saved.value = _state(saved, "unused", [], False)
    assert _state(saved, "unused", [], True)["windows"] == []


def test_recovery_state_requires_complete_original_window_coverage():
    class Saved:
        value = None

        def load_recovery(self, *_args):
            return self.value

    saved = Saved()
    original = [{"target_start": 0, "target_end": 1}, {"target_start": 1, "target_end": 2}]
    saved.value = _state(saved, "unused", original, False)
    saved.value["windows"] = original[:1]
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_invalid"):
        _state(saved, "unused", original, True)


def test_retained_overview_fragments_stay_in_source_order():
    first = {"target_start": 0, "target_end": 1}
    second_child = {"target_start": 1, "target_end": 2}
    from openkb.agent.document_window_receipts import window_receipt_id

    state = {
        "windows": [first, second_child],
        "fragments": {window_receipt_id(first): "First section."},
        "retained_fragments": [{"start": 1, "text": "Second section."}],
    }
    assert ordered_fragments(state) == ["First section.", "Second section."]


def test_unique_exposed_catalog_title_can_update_existing_page(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts/notes.md").write_text("# Notes\n\nExisting note.")
    catalog = _catalog_entries(wiki, {"concepts/notes"})
    assert catalog == [("concepts/notes", "Notes", "Existing note.")]
    result = accept_pages(
        "- 名称：Notes\n  类别：concept\n  主体章节：操作",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets={"concepts/notes"},
        allowed_update_targets={"concepts/notes"},
        catalog_titles={"concepts/notes": "Notes"},
    )
    assert result.pages[0].target == "concepts/notes"


def test_hidden_duplicate_title_cannot_authorize_existing_page_update():
    result = accept_pages(
        "- 名称：Notes\n  类别：concept\n  主体章节：操作",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets={"concepts/notes", "concepts/notes-copy"},
        allowed_update_targets={"concepts/notes"},
        catalog_titles={"concepts/notes": "Notes", "concepts/notes-copy": "Notes"},
    )
    assert result.pages[0].target == ""
    assert result.pages[0].name not in {"concepts/notes", "concepts/notes-copy"}


def test_page_planning_notes_reach_generation_as_unverified_hints():
    from openkb.agent.document_pages import _page_fields

    result = accept_pages(
        "- 名称：操作\n  类别：concept\n  主体章节：操作\n  外部参考：启动前须遵循外部审批手册",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert _page_fields(result.pages[0])["planning_notes"] == ["启动前须遵循外部审批手册"]


def test_empty_planning_coverage_rejects_malformed_persisted_ranges():
    coverage = planning_coverage(None, _parsed(), outcome="empty")
    assert coverage["status"] == "empty"
    coverage["missing_ranges"] = [{"block_id": "bad"}]
    with pytest.raises(ValueError, match="missing range"):
        validate_planning_coverage(coverage)


def test_explicit_zero_pages_is_reported_as_a_finished_recommendation(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)

    def respond(messages, *, settings):
        subtask = json.loads(messages[-1]["content"])["subtask"]
        return "资料概览说明前提和操作。" if subtask == "overview" else "无需新增页面。"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            return_result=True,
            plan_only=True,
        )
    assert result.outcome == "complete" and result.plan is not None
    assert result.plan.pages == []
    assert json.loads(Path(result.report_ref).read_text())["no_pages_recommended"] is True


def test_reordered_table_columns_and_explicit_entity_type():
    parsed = _parsed()
    table = """| Section key / heading path | Kind | Page title |
|---|---|---|
| `section:run` / 操作 | Concept | 执行流程 |
| `section:pre` / 前提 | Product | 凭据工具 |
"""
    result = accept_pages(
        table,
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=["product"],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [(page.kind, page.scope_resolution) for page in result.pages] == [
        ("concept", "section"),
        ("entity", "section"),
    ]
    assert result.pages[1].type == "product"


def test_table_uses_evidence_section_column_for_precise_location():
    result = accept_pages(
        "| 页面名称 | 类型 | 依据章节 |\n|---|---|---|\n| 执行流程 | 概念 | 操作 |",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert result.pages[0].scope_resolution == "section"
    assert result.pages[0].subject_ranges == [[1, 2]]


def test_table_accepts_combined_page_title_header():
    result = accept_pages(
        "| 页面名称/标题 | 类型 | 依据的 section_key 或标题路径 |\n"
        "|---|---|---|\n| 执行流程 | Concept | section:run / 操作 |",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert [(page.title, page.subject_ranges) for page in result.pages] == [("执行流程", [[1, 2]])]


def test_table_source_location_column_resolves_embedded_section_key():
    result = accept_pages(
        "| 页面标题 | 类型 | 来源位置 |\n|---|---|---|\n"
        "| 执行流程 | concept | section:run（heading path：操作） |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.rejected
    assert result.pages[0].subject_ranges == [[1, 2]]
    assert result.pages[0].scope_resolution == "section"


def test_table_page_and_page_type_headers_are_recognized():
    result = accept_pages(
        "| 页面 | 页面类型 | 依据章节 |\n|---|---|---|\n| 执行流程 | concept | section:run |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert [page.title for page in result.pages] == ["执行流程"]
    assert not result.rejected


def test_compact_dash_list_accepts_kind_and_section_key():
    result = accept_pages(
        "- **执行流程** — concept，section_key: `section:run`，"
        "heading path: 操作\n"
        "- 背景 — concept, section_key: `section:pre` — 补充说明",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [(page.title, page.subject_ranges) for page in result.pages] == [
        ("执行流程", [[1, 2]]),
        ("背景", [[0, 1]]),
    ]


def test_quoted_title_and_plain_title_are_the_same_page():
    result = accept_pages(
        "- 「执行流程」 — concept — section:run\n- 执行流程 — concept — section:run",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [page.title for page in result.pages] == ["执行流程"]


def test_later_window_ignores_exact_echo_of_accepted_page():
    parsed = _parsed()
    earlier = accept_pages(
        "- 凭据准备 — concept — section:pre",
        navigation=_navigation(),
        target=[[0, 1]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    later = accept_pages(
        "- 「凭据准备」 — concept — section:pre",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        accepted=earlier.pages,
    )
    assert not later.pages
    assert not later.rejected


def test_later_window_does_not_suppress_a_conflicting_name():
    parsed = _parsed()
    earlier = accept_pages(
        '[{"name":"旧名","title":"共享标题","kind":"concept","section":"section:pre"}]',
        navigation=_navigation(),
        target=[[0, 1]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    later = accept_pages(
        '[{"name":"新名","title":"共享标题","kind":"concept","section":"section:pre"}]',
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        accepted=earlier.pages,
    )
    assert len(later.pages) == 1 and not later.rejected
    assert later.pages[0].name != earlier.pages[0].name


def test_later_window_ignores_a_subset_echo_of_accepted_page():
    parsed = _parsed()
    earlier = accept_pages(
        "- 凭据准备 — concept — section:pre",
        navigation=_navigation(),
        target=[[0, 1]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    earlier.pages[0].subject_ranges.append([1, 2])
    later = accept_pages(
        "- 凭据准备 — concept — section:pre",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        accepted=earlier.pages,
    )
    assert not later.pages
    assert not later.rejected


def test_later_window_ignores_echo_bound_to_existing_catalog_target():
    parsed = _parsed()
    target_path = "concepts/credential-article"
    arguments = dict(
        navigation=_navigation(),
        parsed=parsed,
        entity_types=[],
        existing_targets={target_path},
        allowed_update_targets={target_path},
        catalog_titles={target_path: "Credential Prep"},
    )
    earlier = accept_pages(
        '[{"name":"Credential Prep","kind":"concept","section":"section:pre"}]',
        target=[[0, 1]],
        **arguments,
    )
    assert earlier.pages[0].name == target_path
    later = accept_pages(
        '[{"name":"Credential Prep","kind":"concept","section":"section:pre"}]',
        target=[[1, 2]],
        accepted=earlier.pages,
        **arguments,
    )
    assert not later.pages
    assert not later.rejected
    assert [row["reason"] for row in later.filtered] == ["accepted_echo"]


def test_english_no_new_page_warranted_is_explicit_empty_plan():
    result = accept_pages(
        "No new page is warranted from this target.",
        navigation=_navigation(),
        target=[[0, 1]],
        parsed=_parsed(),
        entity_types=[],
        existing_targets=set(),
    )
    assert result.no_pages
    assert not result.rejected


def test_mixed_language_titles_do_not_collide_on_ascii_fragment():
    parsed = _parsed()
    result = accept_pages(
        "## 实体页\n- 名称：Gluster资料甲.docx\n  类型：work\n"
        "- 名称：Gluster资料乙.docx\n  类型：work\n",
        navigation=_navigation(),
        target=[[0, 1]],
        parsed=parsed,
        entity_types=["work"],
        existing_targets=set(),
    )
    assert len(result.pages) == 2
    assert result.pages[0].name != result.pages[1].name


def test_explanatory_bullets_are_not_page_candidates():
    parsed = _parsed()
    text = """## 说明
- 以下建议仅使用已有原文。
## 页面计划
| 名称 | 类型 | 主体章节 |
| --- | --- | --- |
| 执行流程 | 概念（Concept） | section:run |
| 凭据工具 | 产品（Product） | section:pre |
"""
    result = accept_pages(
        text,
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=["product"],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [(page.kind, page.title) for page in result.pages] == [
        ("concept", "执行流程"),
        ("entity", "凭据工具"),
    ]


def test_inline_pipe_list_accepts_section_key_and_other_entity_type():
    parsed = _parsed()
    result = accept_pages(
        "- 执行流程 | 概念 | section_key: section:run\n"
        "- 辅助命令 | 其他 | section_key: section:pre",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=["other"],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [(page.kind, page.type) for page in result.pages] == [
        ("concept", None),
        ("entity", "other"),
    ]


def test_bold_field_labels_and_em_dash_list():
    parsed = _parsed()
    result = accept_pages(
        "- **前提** — concept — heading path: 文档 > 前提 (section_key: section:pre)\n"
        "- **页面名称**：执行流程\n  - **类型**：concept\n"
        "  - **章节/标题路径**：前提 / 操作\n"
        "  - **说明/范围**：保留先决条件。",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    )
    assert not result.rejected
    assert [page.title for page in result.pages] == ["前提", "执行流程"]
    assert result.pages[1].subject_ranges == [[0, 1], [1, 2]]


def test_cited_unread_work_stays_a_reference_hint():
    parsed = _parsed()
    parsed.blocks[1].text += " 启动前须遵循《外部审批手册》。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    evidence = {"blocks": [{"text": parsed.blocks[1].text}]}
    result = accept_pages(
        "| 名称 | 类型 | 主体章节 |\n|---|---|---|\n"
        "| 执行流程 | concept | section:run |\n"
        "| 外部审批手册 | work | 操作（引用处） |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=["work"],
        existing_targets=set(),
        evidence=evidence,
    )
    assert [page.title for page in result.pages] == ["执行流程"]
    assert result.rejected == []


def test_cited_work_with_supplied_body_can_be_planned():
    parsed = _parsed()
    parsed.blocks[1].text = "《示例手册》正文：工具按步骤执行。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    result = accept_pages(
        "| 名称 | 类型 | 主体章节 |\n|---|---|---|\n| 示例手册 | work | section:run |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=["work"],
        existing_targets=set(),
        evidence={"blocks": [{"text": parsed.blocks[1].text}]},
    )
    assert [page.title for page in result.pages] == ["示例手册"]
    assert not result.rejected


@pytest.mark.parametrize("title", ["外部审批手册", "《外部审批手册》"])
def test_explicitly_unsupplied_reference_is_not_a_concept_page(title):
    parsed = _parsed()
    parsed.blocks[1].text += " 操作前须遵循《外部审批手册》；该手册未随本文提供。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    evidence = {"blocks": [{"text": parsed.blocks[1].text}]}
    result = accept_pages(
        "| 名称 | 类型 | 主体章节 |\n|---|---|---|\n"
        "| 执行流程 | concept | section:run |\n"
        f"| {title} | concept | section:run |",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        evidence=evidence,
    )
    assert [page.title for page in result.pages] == ["执行流程"]
    assert result.rejected == []


@pytest.mark.parametrize("body", ["## 标题", "```markdown\n# 标题\n```", "待补充"])
def test_overview_rejects_title_or_placeholder(body):
    assert not accept_overview(body).text


def test_overview_survives_page_failure_and_resume(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def mock_caller(messages, *, settings):
        task = json.loads(messages[-1]["content"])["subtask"]
        calls.append(task)
        return "这份资料说明凭据准备和执行前提。" if task == "overview" else "无法判断"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=mock_caller,
            return_result=True,
            plan_only=True,
        )
        assert result.outcome == "partial"
        assert result.overview_ref
        assert "凭据准备" in Path(result.overview_ref).read_text(encoding="utf-8")
        assert result.report_ref and Path(result.report_ref).is_file()
        again = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=mock_caller,
            return_result=True,
            plan_only=True,
            resume=True,
        )
        assert again.outcome == "partial"
        assert calls == ["overview", "pages", "pages", "pages"]


def test_resume_after_execution_error_does_not_regenerate_saved_overview(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def fail_pages(messages, *, settings):
        task = json.loads(messages[-1]["content"])["subtask"]
        calls.append(task)
        if task == "pages":
            raise RuntimeError("service unavailable")
        return "凭据准备是执行前提。"

    def complete_pages(messages, *, settings):
        task = json.loads(messages[-1]["content"])["subtask"]
        calls.append(task)
        return "- 名称：执行流程\n  类别：概念"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        with pytest.raises(RuntimeError, match="service unavailable"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                SETTINGS,
                checkpoints,
                mock_caller=fail_pages,
                plan_only=True,
            )
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=complete_pages,
            plan_only=True,
            return_result=True,
            resume=True,
        )
    assert calls == ["overview", "pages", "pages"]
    assert result.outcome == "complete"


def test_overview_and_page_plan_are_independent_v3_artifacts(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def mock_caller(messages, *, settings):
        task = json.loads(messages[-1]["content"])["subtask"]
        calls.append(task)
        if task == "overview":
            return "凭据是执行前提。"
        return "- 名称：执行流程\n  类别：概念"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=mock_caller,
            return_result=True,
            plan_only=True,
        )
    assert calls == ["overview", "pages"]
    assert result.outcome == "complete"
    assert result.plan and len(result.plan.pages) == 1
    assert result.plan.pages[0].scope_resolution is None
    assert result.plan.pages[0].state == "pending_evidence"
    assert from_dict(to_dict(result.plan)).metadata["protocol"] == "document-plan-v4"
    assert result.overview_ref and Path(result.overview_ref).is_file()
    assert result.report_ref and Path(result.report_ref).is_file()
    assert not list((workspace / "wiki").rglob("*.md"))


def test_v3_coverage_separates_precise_and_fallback_ranges():
    parsed = _parsed()
    exact = accept_pages(
        "- 名称：前提说明\n  类别：概念\n  主体章节：前提",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    ).pages[0]
    broad = accept_pages(
        "- 名称：执行流程\n  类别：概念",
        navigation=_navigation(),
        target=[[0, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    ).pages[0]
    exact.state = broad.state = "ready"
    broad.subject_ranges = [[0, 2]]
    broad.scope_resolution = "target_fallback"
    plan = DocumentPlan(
        metadata={"protocol": "document-plan-v3", "outcome": "complete"},
        overview=OverviewPlan(text="资料概览"),
        pages=[exact, broad],
    )
    coverage = planning_coverage(plan, parsed)
    validate_planning_coverage(coverage)
    assert coverage["precise_chars"] == parsed.blocks[0].chars
    assert coverage["fallback_chars"] == parsed.blocks[1].chars
    assert coverage["unrouted_chars"] == 0


def test_internal_reference_adds_cross_window_original_context():
    parsed = _parsed()
    parsed.blocks[1].text = "先按照本文档的“前提”检查，再执行操作。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    page = accept_pages(
        "- 名称：执行流程\n  类别：概念\n  Section: section:run",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    ).pages[0]
    evidence = {
        "parse_id": parsed.id,
        "blocks": [{"id": parsed.blocks[1].id, "order": 1, "text": parsed.blocks[1].text}],
    }
    navigation = {
        "nodes": [
            {
                "id": "pre",
                "title": "前提",
                "parent": None,
                "start": 0,
                "end": 1,
            }
        ]
    }
    assert _source_references(evidence, navigation, parsed, [page]) == []
    assert page.context_ranges == [[0, 1]]


def test_informational_reference_does_not_become_required_context():
    parsed = _parsed()
    parsed.blocks[1].text = "扩展阅读参见“前提”，不影响本步骤执行。"
    parsed.blocks[1].chars = len(parsed.blocks[1].text)
    page = accept_pages(
        "- 名称：执行流程\n  类别：概念",
        navigation=_navigation(),
        target=[[1, 2]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
    ).pages[0]
    evidence = {
        "parse_id": parsed.id,
        "blocks": [{"id": parsed.blocks[1].id, "order": 1, "text": parsed.blocks[1].text}],
    }
    navigation = {
        "nodes": [
            {
                "id": "pre",
                "title": "前提",
                "parent": None,
                "start": 0,
                "end": 1,
            }
        ]
    }
    _source_references(evidence, navigation, parsed, [page])
    assert page.context_ranges == []


def test_truncated_overview_keeps_complete_first_fragment(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def respond(messages, *, settings):
        subtask = json.loads(messages[-1]["content"])["subtask"]
        calls.append(subtask)
        if subtask == "pages":
            return "No new pages needed."
        if calls.count("overview") == 1:
            return _Response("The source states a prerequisite.\n\nUnfinished sentence", "length")
        return "The operation follows that prerequisite."

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    assert calls == ["overview", "overview", "pages"]
    assert result.outcome == "complete"
    assert Path(result.overview_ref).read_text() == ("The operation follows that prerequisite.\n")
    assert result.plan and not result.plan.pages


def test_retained_partial_overview_has_its_own_report_count(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    settings = {**SETTINGS, "processing": {**SETTINGS["processing"], "max_attempts": 1}}

    def respond(messages, *, settings):
        subtask = json.loads(messages[-1]["content"])["subtask"]
        if subtask == "overview":
            return _Response("A complete paragraph.\n\nunfinished", "length")
        return "无需新增页面。"

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    report = json.loads(Path(result.report_ref).read_text())
    assert result.outcome == "partial"
    assert report["overview_targets"] == {"complete": 0, "partial": 1, "failed": 0, "total": 1}


def test_rejected_page_candidate_does_not_erase_accepted_page(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    pages_calls = 0

    def respond(messages, *, settings):
        nonlocal pages_calls
        subtask = json.loads(messages[-1]["content"])["subtask"]
        if subtask == "overview":
            return "The source describes two related items."
        pages_calls += 1
        if pages_calls == 1:
            return "- Name: Good\n  Kind: concept\n- Name: Bad\n  Kind: entity"
        return "- Name: Bad\n  Kind: entity\n  Type: product"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    assert pages_calls == 1
    assert result.outcome == "complete"
    assert {page.title for page in result.plan.pages} == {"Good"}
    assert [row["title"] for row in result.plan.metadata["deferred_suggestions"]] == ["Bad"]
    report = json.loads(Path(result.report_ref).read_text())
    assert report["rejected_candidates"] == []
    assert not report["rejected_candidate_history"]


def test_planning_report_counts_filtered_reference_hints(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)

    def respond(messages, *, settings):
        subtask = json.loads(messages[-1]["content"])["subtask"]
        if subtask == "overview":
            return "The source describes an operation and a cited work."
        return (
            "- Name: Good\n  Kind: concept\n"
            "- Name: Cited Manual\n  Kind: concept\n  Section: 操作（引用处）"
        )

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    report = json.loads(Path(result.report_ref).read_text())
    assert [page.title for page in result.plan.pages] == ["Good"]
    assert report["filtered_candidates"] == {"unsupplied_reference": 1}
    assert report["filtered_candidate_history"][0]["window"].startswith("global-pages:")


def test_empty_planning_result_has_report_without_fake_overview(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=lambda *_args, **_kwargs: "无法判断",
            plan_only=True,
            return_result=True,
        )
    assert result.outcome == "empty" and result.plan is None
    assert result.overview_ref is None
    assert Path(result.report_ref).is_file()
    assert not list((workspace / "wiki").rglob("*.md"))


def test_a_and_b_share_source_prefix_and_dispatch_without_json_format(monkeypatch):
    evidence = {"group_id": "g", "blocks": [{"id": "b", "text": "Original."}]}
    args = (evidence, {}, {"target_start": 0, "target_end": 1}, [], "", [], "")
    overview = plan_messages(*args, subtask="overview")
    pages = plan_messages(*args, subtask="pages")
    assert overview[0] == pages[0]
    assert (
        overview[1]["content"].split('"plan_protocol":', 1)[0]
        == (pages[1]["content"].split('"plan_protocol":', 1)[0])
    )
    captured = []
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.compiler._llm_call",
        lambda _model, _messages, _stage, **kwargs: captured.append(kwargs) or "Markdown",
    )
    _call(overview, SETTINGS, RequestLimits.from_config(SETTINGS), None, None, "overview")
    _call(pages, SETTINGS, RequestLimits.from_config(SETTINGS), None, None, "pages")
    assert all(row["decode_response"] is False for row in captured)
    assert all("response_format" not in row for row in captured)


def test_capacity_retry_preserves_overview_without_restoring_per_window_pages(
    tmp_path,
    monkeypatch,
):
    source, parsed = _DummySource(), _parsed()
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr("litellm.token_counter", lambda **_: 100)
    calls = []

    def respond(messages, *, settings):
        task = json.loads(messages[-1]["content"])
        subtask = task["subtask"]
        start = task["target"].get("target_start", 0)
        calls.append((subtask, start))
        if subtask == "overview":
            return "A prerequisite precedes the operation."
        if len([row for row in calls if row[0] == "pages"]) == 1:
            raise InputTooLarge()
        return f"- Name: Topic {start}\n  Kind: concept"

    with CompilationCheckpoints(tmp_path, source, parsed, SETTINGS, None) as checkpoints:
        result = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            SETTINGS,
            checkpoints,
            mock_caller=respond,
            plan_only=True,
            return_result=True,
        )
    assert calls[0] == ("overview", 0)
    assert [row[0] for row in calls].count("overview") == 1
    assert result.plan and len(result.plan.pages) == 1
    assert result.plan.overview.text.strip() == "A prerequisite precedes the operation."
    assert result.outcome == "complete"
