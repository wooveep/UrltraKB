"""A stale TOC must not override an evidenced heading in the document body."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from pageindex.index import page_index as pi
from pageindex.index.utils import post_processing


def sample():
    toc = [
        {"structure": "4.4.2", "title": "管理平台单节点部署", "physical_index": 83},
        {"structure": "4.4.3", "title": "环境清理", "physical_index": 84},
        {"structure": "4.4.4", "title": "环境查看安装配置", "physical_index": 84},
    ]
    pages = [
        ("4.4.2 管理平台单节点部署\n安装步骤", 10),
        (
            "安装手册\n4.4.3 卸载管理平台\n执行命令，清理已安装的管理平台\n"
            "4.4.4 环境查看安装配置\n查看配置",
            20,
        ),
    ]
    incorrect = [{"list_index": 1, "title": "环境清理", "answer": "no", "page_number": 84}]
    evidence = {
        "same_section": True,
        "physical_index": 84,
        "heading": "4.4.3 卸载管理平台",
        "title": "卸载管理平台",
    }
    return toc, pages, incorrect, evidence


def run_repair(monkeypatch, evidence=None, *, check="no", failure=None):
    toc, pages, incorrect, good = sample()
    monkeypatch.setattr(
        pi, "single_toc_item_index_fixer", AsyncMock(return_value=84, side_effect=failure)
    )
    monkeypatch.setattr(pi, "check_title_appearance", AsyncMock(return_value={"answer": check}))
    complete = AsyncMock(
        return_value=json.dumps(good if evidence is None else evidence, ensure_ascii=False)
    )
    monkeypatch.setattr(pi, "llm_acompletion", complete)
    result = asyncio.run(
        pi.fix_incorrect_toc_with_retries(
            toc, pages, incorrect, start_index=83, max_attempts=1, model="test", logger=Mock()
        )
    )
    return result, complete


def test_body_heading_replaces_stale_toc_and_survives_tree_conversion(monkeypatch):
    (toc, incorrect), complete = run_repair(monkeypatch)
    assert incorrect == []
    assert toc[1]["title"] == "卸载管理平台"
    assert toc[1]["physical_index"] == 84
    assert toc[1]["title_correction"] == {
        "toc_title": "环境清理",
        "body_heading": "4.4.3 卸载管理平台",
        "physical_index": 84,
    }
    tree = post_processing(copy.deepcopy(toc), 84)
    assert tree[1]["title_correction"] == toc[1]["title_correction"]
    assert complete.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"title": "删除整个集群"},
        {"heading": "4.4.4 环境查看安装配置", "title": "环境查看安装配置"},
        {"physical_index": 82},
        {"physical_index": 85},
        {"physical_index": True},
        {"same_section": False},
        {"heading": "请看 4.4.3 卸载管理平台"},
        {"heading": "4.4.3 卸载管理平台......84"},
    ],
)
def test_unevidenced_or_neighboring_heading_is_not_accepted(monkeypatch, change):
    evidence = sample()[3] | change
    (toc, incorrect), _ = run_repair(monkeypatch, evidence)
    assert toc[1]["title"] == "环境清理"
    assert len(incorrect) == 1
    assert "title_correction" not in toc[1]


def test_regular_page_number_repair_does_not_request_title_change(monkeypatch):
    (toc, incorrect), complete = run_repair(monkeypatch, check="yes")
    assert incorrect == []
    assert toc[1]["title"] == "环境清理"
    complete.assert_not_awaited()


@pytest.mark.parametrize("response", [[], "not an object", {}, {"same_section": "true"}])
def test_malformed_response_stays_unresolved(monkeypatch, response):
    (_, incorrect), _ = run_repair(monkeypatch, response)
    assert len(incorrect) == 1


def test_failed_repair_remains_in_unresolved_results(monkeypatch):
    (_, incorrect), _ = run_repair(monkeypatch, failure=RuntimeError("provider unavailable"))
    assert len(incorrect) == 1
    assert incorrect[0]["list_index"] == 1


def test_invalid_candidate_page_is_not_indexed(monkeypatch):
    complete = AsyncMock()
    monkeypatch.setattr(pi, "llm_acompletion", complete)
    result = asyncio.run(
        pi.check_title_appearance(
            {"title": "标题", "physical_index": 82, "list_index": 0}, [("正文", 1)], start_index=83
        )
    )
    assert result["answer"] == "no"
    complete.assert_not_awaited()
