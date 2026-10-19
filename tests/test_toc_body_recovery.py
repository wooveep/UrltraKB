"""A stale TOC cannot dictate the identity or children of a body chapter."""

import asyncio
import copy
import json
import re

import pytest
from pageindex.config import IndexConfig
from pageindex.errors import IndexQualityError
from pageindex.index import page_index as pi
from pageindex.index.pipeline import _NullLogger
from pageindex.index.utils import post_processing
from pageindex.llm import IndexResponse, llm_client_scope


@pytest.fixture(autouse=True)
def scalar_node_protocol():
    """Keep localization regressions on the supported per-node rollback protocol."""
    from pageindex.index.node_batches import node_batch_scope

    with node_batch_scope(1):
        yield


def incident():
    pages = [
        ("4.19 备份\n备份操作。\n", 20),
        ("备份操作的结尾。\n4.21\n系统管理\n4.21.6 用户管理\n用户操作。\n", 40),
        ("4.21.7 角色管理\n角色操作。\n4.21.8 新功能\n新增正文。\n", 40),
        ("5 审计\n审计操作。\n", 20),
        ("6 附录\n附录正文。\n", 20),
    ]
    toc = [
        {"structure": section, "title": title, "physical_index": page}
        for section, title, page in [
            ("4.19", "备份", 1295),
            ("4.20", "系统", 1296),
            ("4.20.1", "用户管理", 1296),
            ("4.20.2", "角色管理", 1297),
            ("5", "审计", 1298),
            ("6", "附录", 1299),
        ]
    ]
    body = [copy.deepcopy(toc[0])]
    body.extend(
        {"structure": section, "title": title, "physical_index": page}
        for section, title, page in [
            ("4.21", "系统管理", 1296),
            ("4.21.6", "用户管理", 1296),
            ("4.21.7", "角色管理", 1297),
            ("4.21.8", "新功能", 1297),
        ]
    )
    body.append(copy.deepcopy(toc[4]))
    return pages, toc, body


class BodyModel:
    """Deliver fixed results at the real PageIndex model boundary; never send."""

    def __init__(self, body):
        self.body = body
        self.calls = []
        self.saved = {}
        self.body_error = None
        self.located_page = 1296
        self.rejected_titles = {"系统"}

    def stage_result(self, name, value=None):
        if value is None:
            return copy.deepcopy(self.saved.get(name))
        self.saved[name] = copy.deepcopy(value)

    def complete(self, messages, *, stage):
        prompt = messages[-1]["content"]
        self.calls.append((stage, prompt))
        if "expert in extracting hierarchical tree structure" in prompt:
            if self.body_error:
                raise self.body_error
            answer = self.body
        elif "TOC entry:" in prompt:
            answer = {"same_section": False}
        elif "Section Title:\n" in prompt:
            answer = {"physical_index": f"<physical_index_{self.located_page}>"}
        elif "start_begin" in prompt:
            answer = {"start_begin": "no"}
        elif "The given section title is " in prompt:
            title = re.search(r"The given section title is (.*?)\.\n", prompt)[1]
            answer = {"answer": "no" if title in self.rejected_titles else "yes"}
        else:
            raise AssertionError("Unexpected offline model request: " + prompt[:100])
        return IndexResponse(json.dumps(answer, ensure_ascii=False))

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)

    @property
    def rebuilds(self):
        return [p for _, p in self.calls if "expert in extracting hierarchical" in p]


def run_navigation(monkeypatch, pages, toc, model, *, start_index=1295):
    monkeypatch.setattr(pi, "process_toc_with_page_numbers", lambda *a, **k: copy.deepcopy(toc))
    opt = IndexConfig(model="gpt-4o", llm_client=model, require_llm_client=True)
    with llm_client_scope(model, required=True):
        return asyncio.run(
            pi.meta_processor(
                pages,
                mode="process_toc_with_page_numbers",
                toc_content="Untrusted original TOC",
                toc_page_list=[],
                start_index=start_index,
                opt=opt,
                logger=_NullLogger(),
            )
        )


def test_body_rebuild_replaces_the_conflicting_chapter_and_its_children(monkeypatch):
    pages, toc, body = incident()
    before = copy.deepcopy(pages)
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)

    assert [(n["structure"], n["title"]) for n in result] == [
        ("4.19", "备份"),
        ("4.21", "系统管理"),
        ("4.21.6", "用户管理"),
        ("4.21.7", "角色管理"),
        ("4.21.8", "新功能"),
        ("5", "审计"),
        ("6", "附录"),
    ]
    assert pages == before
    assert len(model.rebuilds) == 1
    assert "<physical_index_1299>" not in model.rebuilds[0]
    tree = post_processing(copy.deepcopy(result), 1299, start_physical_index=1295)
    chapter = next(n for n in tree if n["structure"] == "4.21")
    assert [n["structure"] for n in chapter["nodes"]] == ["4.21.6", "4.21.7", "4.21.8"]
    assert chapter["toc_recovery"]["entries"][0]["title"] == "系统"
    assert tree[-1]["title"] == "附录"


def test_number_only_conflict_also_rebuilds_the_body_subtree(monkeypatch):
    pages, toc, body = incident()
    toc[1]["title"] = "系统管理"
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    assert [n["structure"] for n in result[1:5]] == ["4.21", "4.21.6", "4.21.7", "4.21.8"]
    assert len(model.rebuilds) == 1


def test_number_only_recovery_reuses_the_delivered_roles_for_the_same_native_headings(monkeypatch):
    pages, toc, body = incident()
    toc[1]["title"] = "系统管理"
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    assert result[1]["structure"] == "4.21"
    for title in ("系统管理", "用户管理", "角色管理"):
        checks = [
            prompt
            for stage, prompt in model.calls
            if stage == "verification" and f"The given section title is {title}.\n" in prompt
        ]
        assert len(checks) == 1
    assert any("The given section title is 新功能.\n" in p for _, p in model.calls)


def test_rebuilt_heading_roles_use_the_unchanged_original_adjacent_pages(monkeypatch):
    pages = [
        ("4.5 主机管理\n之前的章节正文。\n", 20),
        ("4.6 虚拟机管理\n4.6.6 虚拟机电源管理\n电源操作。\n", 40),
        ("4.7 虚拟化拓扑\n拓扑操作。\n", 20),
    ]
    toc = [
        {"structure": s, "title": t, "physical_index": p}
        for s, t, p in [
            ("4.5", "主机管理", 1),
            ("4.6", "虚拟机管理", 2),
            ("4.6.1", "过时电源", 2),
            ("4.7", "虚拟化拓扑", 3),
        ]
    ]
    body = [
        {"structure": s, "title": t, "physical_index": p}
        for s, t, p in [
            ("4.6", "虚拟机管理", 2),
            ("4.6.6", "虚拟机电源管理", 2),
            ("4.7", "虚拟化拓扑", 3),
        ]
    ]
    model = BodyModel(body)
    model.located_page = 2
    model.rejected_titles.add("过时电源")
    run_navigation(monkeypatch, pages, toc, model, start_index=1)
    checks = [
        prompt
        for stage, prompt in model.calls
        if stage == "verification" and "The given section title is 虚拟机电源管理.\n" in prompt
    ]
    assert len(checks) == 1
    assert "<physical_index_1>" in checks[0]
    assert "之前的章节正文。" in checks[0]


def test_a_fuzzy_yes_cannot_keep_a_toc_title_absent_from_the_body(monkeypatch):
    pages, toc, body = incident()
    del toc[2:4]
    model = BodyModel(body)
    model.rejected_titles.clear()
    result = run_navigation(monkeypatch, pages, toc, model)
    assert "系统" not in [n["title"] for n in result]
    assert "系统管理" in [n["title"] for n in result]
    assert len(model.rebuilds) == 1


def test_wrapped_body_titles_keep_their_native_number_and_full_source_span(monkeypatch):
    pages, toc, body = incident()
    pages[1] = (pages[1][0].replace("系统管理", "系统\n管理"), 40)
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    chapter = next(n for n in result if n["structure"] == "4.21")
    anchor = chapter["anchor"]
    assert anchor["excerpt"] == "4.21\n系统\n管理"
    assert pages[1][0][slice(*anchor["range"])] == anchor["excerpt"]


@pytest.mark.parametrize("stale_chapter", [False, True])
def test_native_pdf_numbers_touching_chinese_titles_match_the_delivered_navigation(
    monkeypatch, stale_chapter
):
    # Retain the failure's native lines from physical pages 13-15. PDF text
    # extraction omitted the spaces that the delivered model response added.
    pages = [
        (
            "第1 章关于文档\n1.1读者对象\n本文档主要适用于以下用户：\n"
            "1.2文档说明\n本文档是为了正确的指导系统管理员。\n"
            "1.3阅读约定\n表1 符号及格式含义\n",
            70,
        ),
        ("符号\n格式含义\n单击\n快速按下并释放鼠标的一个按钮。", 20),
        ("第2 章总体介绍\n2.1 产品简介\n产品正文。\n2.2 适用对象\n用户正文。", 50),
    ]
    sections = [
        ("1", "关于文档", 13),
        ("1.1", "读者对象", 13),
        ("1.2", "文档说明", 13),
        ("1.3", "阅读约定", 13),
        ("2", "总体介绍", 15),
        ("2.1", "产品简介", 15),
        ("2.2", "适用对象", 15),
    ]
    toc = [
        {"structure": section, "title": title, "physical_index": page}
        for section, title, page in sections
    ]
    # The real saved recovery reply included both the conflict and its next
    # chapter as context, with section numbers repeated in the returned titles.
    body = [
        {
            "structure": section,
            "title": f"第{section}章 {title}" if "." not in section else f"{section} {title}",
            "physical_index": f"<physical_index_{page}>",
        }
        for section, title, page in sections
    ]
    model = BodyModel(body)
    model.located_page = 13
    if stale_chapter:
        toc[0]["title"] = "旧文档概述"
        model.rejected_titles.add("旧文档概述")
    before = copy.deepcopy(pages)
    result = run_navigation(monkeypatch, pages, toc, model, start_index=13)
    assert pages == before
    assert [n["structure"] for n in result] == [s for s, _, _ in sections]
    assert len(model.rebuilds) == int(stale_chapter)
    for item in result[:4]:
        anchor = item["anchor"]
        assert pages[0][0][slice(*anchor["range"])] == anchor["excerpt"]
        assert anchor["evidence"] == "text_match"
    tree = post_processing(copy.deepcopy(result), 15, start_physical_index=13)
    assert [n["structure"] for n in tree[0]["nodes"]] == ["1.1", "1.2", "1.3"]
    assert len(tree) == 2


@pytest.mark.parametrize(
    ("line", "title", "section"),
    [
        ("1.1读者对象", "1.1 读者对象", "1.1"),
        ("1.1读者对象", "1.1读者对象", "1.1"),
        ("1.12文档说明", "文档说明", "1.12"),
        ("12阅读约定", "12 阅读约定", "12"),
        ("1.3阅读\n约定", "1.3 阅读约定", "1.3"),
    ],
)
def test_literal_recovery_preserves_compact_numbered_pdf_title_spans(line, title, section):
    from pageindex.index.toc_recovery import _literal_entries

    items = [{"structure": section, "title": title, "physical_index": 13}]
    result = _literal_entries(items, [(line, 0)], 13, (13, 0), (14, 0), 4)
    assert result[0]["anchor"]["section"] == section
    assert result[0]["anchor"]["excerpt"] == line
    assert result[0]["anchor"]["range"] == [0, len(line)]


def test_compact_pdf_titles_cannot_override_the_literal_section_number():
    from pageindex.index.toc_recovery import _literal_entries

    with pytest.raises(IndexQualityError, match="unambiguous body heading"):
        _literal_entries(
            [{"structure": "1.2", "title": "读者对象", "physical_index": 13}],
            [("1.1读者对象", 0)],
            13,
            (13, 0),
            (14, 0),
            4,
        )


@pytest.mark.parametrize(
    "line", ["1478", "1.12.3", "99.9%", "1.2e3", "请阅读1.1读者对象", "1.1读者对象……13"]
)
def test_compact_title_syntax_does_not_consume_numbers_or_toc_rows(line):
    from pageindex.index.physical_sections import heading

    assert heading(line) is None


def test_deleted_toc_child_does_not_survive_the_body_rebuild(monkeypatch):
    pages, toc, body = incident()
    toc.insert(4, {"structure": "4.20.3", "title": "过时功能", "physical_index": 1297})
    model = BodyModel(body)
    model.rejected_titles.add("过时功能")
    result = run_navigation(monkeypatch, pages, toc, model)
    assert "过时功能" not in [n["title"] for n in result]
    assert "新功能" in [n["title"] for n in result]


def test_already_renumbered_children_do_not_cut_off_the_conflicting_parent(monkeypatch):
    pages, toc, body = incident()
    toc[2]["structure"] = "4.21.6"
    toc[3]["structure"] = "4.21.7"
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    assert [n["structure"] for n in result[1:5]] == ["4.21", "4.21.6", "4.21.7", "4.21.8"]
    assert len(model.rebuilds) == 1


def test_chinese_chapter_heading_is_a_body_neighbor_not_part_of_recovery(monkeypatch):
    pages, toc, body = incident()
    pages[3] = ("第5 章云平台安全保密员\n安全审计正文。\n", 30)
    toc[4]["title"] = body[-1]["title"] = "云平台安全保密员"
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    assert result[-2]["structure"] == "5"
    assert result[-2]["title"] == "云平台安全保密员"
    assert "toc_recovery" not in result[-2]
    assert "<physical_index_1299>" not in model.rebuilds[0]


def test_next_chapter_on_the_same_page_remains_outside_the_rebuilt_subtree(monkeypatch):
    pages, toc, body = incident()
    pages[2] = (pages[2][0] + "5 审计\n审计操作。\n", 50)
    pages[3] = ("审计操作续文。\n", 20)
    toc[4]["physical_index"] = body[-1]["physical_index"] = 1297
    model = BodyModel(body)
    result = run_navigation(monkeypatch, pages, toc, model)
    tree = post_processing(copy.deepcopy(result), 1299, start_physical_index=1295)
    chapter = next(n for n in tree if n["structure"] == "4.21")
    assert [n["structure"] for n in chapter["nodes"]] == ["4.21.6", "4.21.7", "4.21.8"]
    assert tree[-2]["structure"] == "5"
    assert "<physical_index_1298>" not in model.rebuilds[0]


def body_checkpoints(model):
    """Only fully verified scopes are publication checkpoints."""
    return {key: value for key, value in model.saved.items() if key.startswith("body-navigation:")}


def test_validated_body_rebuild_is_retained_for_a_later_attempt(monkeypatch):
    pages, toc, body = incident()
    model = BodyModel(body)
    first = run_navigation(monkeypatch, pages, toc, model)
    rebuild_count = len(model.rebuilds)
    second = run_navigation(monkeypatch, pages, toc, model)
    assert second == first
    assert len(model.rebuilds) == rebuild_count == 1


def test_an_invalid_local_checkpoint_rebuilds_only_its_scope(monkeypatch):
    pages, toc, body = incident()
    model = BodyModel(body)
    expected = run_navigation(monkeypatch, pages, toc, model)
    assert len(body_checkpoints(model)) == 1
    checkpoint = next(iter(body_checkpoints(model)))
    model.saved[checkpoint][0]["title"] = "已失效的标题"
    result = run_navigation(monkeypatch, pages, toc, model)
    assert result == expected
    assert len(model.rebuilds) == 1  # The verified fragment repairs the stale scope checkpoint.


def test_identical_rebuilt_navigation_does_not_repeat_a_rejected_heading_verdict(monkeypatch):
    pages, toc, body = incident()
    model = BodyModel(body)
    model.rejected_titles.add("系统管理")
    with pytest.raises(IndexQualityError) as failure:
        run_navigation(monkeypatch, pages, toc, model)
    assert getattr(failure.value, "local_recovery_exhausted", False)
    checks = [
        prompt
        for stage, prompt in model.calls
        if stage == "verification" and "The given section title is 系统管理.\n" in prompt
    ]
    assert len(checks) == 1
    assert "no progress" in str(failure.value).lower()


def test_a_literal_number_collision_cannot_be_saved_as_a_successful_local_rebuild(monkeypatch):
    pages, toc, body = incident()
    # A body can itself repeat a number. That native spelling must not become
    # two indistinguishable section identities in the published tree.
    pages[1] = (pages[1][0].replace("4.21", "4.19"), 40)
    pages[2] = (pages[2][0].replace("4.21", "4.19"), 40)
    for item in body[1:-1]:
        item["structure"] = item["structure"].replace("4.21", "4.19")
    model = BodyModel(body)
    with pytest.raises(IndexQualityError) as failure:
        run_navigation(monkeypatch, pages, toc, model)
    assert getattr(failure.value, "local_recovery_exhausted", False)
    assert not body_checkpoints(model)


def test_reformatting_the_same_rejected_source_heading_is_not_progress(monkeypatch):
    pages, toc, body = incident()

    class ReformattingBody(BodyModel):
        def complete(self, messages, *, stage):
            if "expert in extracting hierarchical tree structure" in messages[-1]["content"]:
                if self.rebuilds:
                    self.body = copy.deepcopy(body)
                    self.body[1]["title"] = "4.21系统管理"
            return super().complete(messages, stage=stage)

    model = ReformattingBody(body)
    model.rejected_titles.update({"系统管理", "4.21系统管理"})
    with pytest.raises(IndexQualityError, match="no progress"):
        run_navigation(monkeypatch, pages, toc, model)
    assert not any(
        "The given section title is 4.21系统管理.\n" in prompt for _, prompt in model.calls
    )
    assert not body_checkpoints(model)


def test_a_local_retry_keeps_positive_roles_when_only_one_candidate_is_removed(monkeypatch):
    pages, toc, body = incident()
    pages[2] = (pages[2][0] + "4.21.9 页眉\n", 50)
    body.insert(-1, {"structure": "4.21.9", "title": "页眉", "physical_index": 1297})

    class FilteredBody(BodyModel):
        def complete(self, messages, *, stage):
            if "expert in extracting hierarchical tree structure" in messages[-1]["content"]:
                if self.rebuilds:
                    self.body = [item for item in body if item["title"] != "页眉"]
            return super().complete(messages, stage=stage)

    model = FilteredBody(body)
    model.rejected_titles.add("页眉")
    result = run_navigation(monkeypatch, pages, toc, model)
    assert "页眉" not in [n["title"] for n in result]
    assert len(model.rebuilds) == 2
    for title in ("系统管理", "新功能"):
        assert (
            sum(
                stage == "verification" and f"The given section title is {title}.\n" in prompt
                for stage, prompt in model.calls
            )
            == 1
        )


@pytest.mark.parametrize("interrupt_later_scope", [False, True])
def test_independent_conflicts_keep_validated_progress_and_ignore_other_stale_numbers(
    monkeypatch, interrupt_later_scope
):
    pages = [
        ("4.1 正常开始\n正文。\n", 20),
        ("4.2 新章A\n正文。\n", 20),
        ("4.3 正常中间\n正文。\n", 20),
        ("4.4 新章B\n正文。\n", 20),
        ("5 附录\n正文。\n", 20),
    ]
    body = [
        {"structure": s, "title": t, "physical_index": p}
        for s, t, p in [
            ("4.1", "正常开始", 1295),
            ("4.2", "新章A", 1296),
            ("4.3", "正常中间", 1297),
            ("4.4", "新章B", 1298),
            ("5", "附录", 1299),
        ]
    ]
    toc = copy.deepcopy(body)
    toc[1]["structure"] = "4.11"
    toc[3]["structure"] = "4.2"  # This will be replaced, so it cannot reserve 4.2.

    class LocalDeliveries(BodyModel):
        broken = interrupt_later_scope

        def complete(self, messages, *, stage):
            prompt = messages[-1]["content"]
            if "expert in extracting hierarchical tree structure" in prompt:
                tags = [int(n) for n in re.findall(r"<physical_index_(\d+)>", prompt)]
                self.body = [
                    copy.deepcopy(item)
                    for item in body
                    if min(tags) <= item["physical_index"] <= max(tags)
                ]
                if self.broken and max(tags) == 1299:
                    self.body[1]["title"] = "不存在的标题"
            return super().complete(messages, stage=stage)

    model = LocalDeliveries(body)
    before = copy.deepcopy(pages)
    if interrupt_later_scope:
        with pytest.raises(IndexQualityError) as failure:
            run_navigation(monkeypatch, pages, toc, model)
        assert getattr(failure.value, "local_recovery_exhausted", False)
        assert len(body_checkpoints(model)) == 1
        model.broken = False
    result = run_navigation(monkeypatch, pages, toc, model)
    assert [n["structure"] for n in result] == ["4.1", "4.2", "4.3", "4.4", "5"]
    assert len(body_checkpoints(model)) == 2
    assert sum("<physical_index_1295>" in prompt for prompt in model.rebuilds) == 1
    assert pages == before


@pytest.mark.parametrize(
    "bad_body", [[], [{"structure": "4.21", "title": "不存在", "physical_index": 1296}]]
)
def test_exhausted_body_recovery_is_terminal(monkeypatch, bad_body):
    pages, toc, _ = incident()
    model = BodyModel(bad_body)
    with pytest.raises(IndexQualityError) as failure:
        run_navigation(monkeypatch, pages, toc, model)
    assert getattr(failure.value, "local_recovery_exhausted", False)
    assert 1 <= len(model.rebuilds) <= 2
    assert not body_checkpoints(model)


@pytest.mark.parametrize("compact_title", [False, True])
def test_body_recovery_keeps_rejected_heading_roles_unpublished(monkeypatch, compact_title):
    pages, toc, body = incident()
    if compact_title:
        pages[1] = (pages[1][0].replace("4.21\n系统管理", "4.21系统管理"), 40)
    model = BodyModel(body)
    model.rejected_titles.add("系统管理")
    with pytest.raises(IndexQualityError) as failure:
        run_navigation(monkeypatch, pages, toc, model)
    assert getattr(failure.value, "local_recovery_exhausted", False)
    assert not body_checkpoints(model)


def test_body_transport_failure_escapes_without_a_quality_retry(monkeypatch):
    pages, toc, body = incident()
    model = BodyModel(body)
    model.body_error = RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_navigation(monkeypatch, pages, toc, model)
    assert len(model.rebuilds) == 1


@pytest.mark.parametrize("old_key", ["navigation", "navigation:body-first-v1"])
def test_an_old_physical_navigation_checkpoint_cannot_override_body_recovery(monkeypatch, old_key):
    from unittest.mock import AsyncMock

    from pageindex.index import pipeline
    from pageindex.parser.protocol import ContentNode, ParsedDocument

    model = BodyModel([])
    model.saved[old_key] = [{"title": "系统", "structure": "4.20"}]
    build = AsyncMock(return_value=[{"title": "系统管理", "structure": "4.21"}])
    monkeypatch.setattr(pipeline, "_content_based_pipeline", build)
    parsed = ParsedDocument(
        doc_name="fixture",
        nodes=[ContentNode(content="4.21 系统管理\n", tokens=10, index=1)],
        metadata={"unit_kind": "page"},
    )
    opt = IndexConfig(
        llm_client=model, if_add_node_summary=False, if_add_doc_description=False, node_batch_size=1
    )
    first = pipeline.build_index(parsed, opt=opt)
    second = pipeline.build_index(parsed, opt=opt)
    assert first["structure"][0]["title"] == "系统管理"
    assert second == first
    assert build.call_count == 1


@pytest.mark.parametrize("failed", [False, True])
def test_real_collection_preserves_body_pages_and_publishes_only_a_valid_rebuild(
    tmp_path, monkeypatch, failed
):
    from pageindex import LocalClient
    from pageindex.parser.protocol import ContentNode, ParsedDocument

    pages, toc, body = incident()
    for item in [*toc, *body]:
        item["physical_index"] -= 1294
    model = BodyModel([] if failed else body)
    model.located_page = 2

    class OriginalPages:
        def supported_extensions(self):
            return [".fixture"]

        def parse(self, path, **kwargs):
            return ParsedDocument(
                doc_name="fixture",
                nodes=[
                    ContentNode(content=text, tokens=tokens, index=i)
                    for i, (text, tokens) in enumerate(pages, 1)
                ],
                metadata={"unit_kind": "page", "unit_count": len(pages)},
            )

    monkeypatch.setattr(
        pi,
        "check_toc",
        lambda *a, **k: {
            "toc_content": "Untrusted original TOC",
            "toc_page_list": [],
            "page_index_given_in_toc": "yes",
        },
    )
    monkeypatch.setattr(pi, "process_toc_with_page_numbers", lambda *a, **k: copy.deepcopy(toc))
    source = tmp_path / "original.fixture"
    source.write_bytes(b"Immutable original source")
    client = LocalClient(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(
            model="gpt-4o",
            llm_client=model,
            node_batch_size=1,
            require_llm_client=True,
            if_add_node_summary=False,
            if_add_doc_description=False,
            if_add_node_text=True,
        ),
    )
    client.register_parser(OriginalPages())
    try:
        col = client.collection()
        if failed:
            with pytest.raises(IndexQualityError) as failure:
                col.add(str(source))
            assert getattr(failure.value, "local_recovery_exhausted", False)
            assert col.list_documents() == []
        else:
            identity = col.add(str(source))
            doc = col.get_document(identity, include_text=True)
            chapter = next(n for n in doc["structure"] if n["structure"] == "4.21")
            assert [n["structure"] for n in chapter["nodes"]] == ["4.21.6", "4.21.7", "4.21.8"]
            assert chapter["toc_recovery"]["entries"][0]["title"] == "系统"
            assert [p["content"] for p in col.get_page_content(identity, "1-5")] == [
                text for text, _ in pages
            ]
            before = len(model.calls)
            assert col.add(str(source)) == identity
            assert len(model.calls) == before
        assert source.read_bytes() == b"Immutable original source"
    finally:
        client.close()
