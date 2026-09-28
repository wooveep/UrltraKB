"""Legacy branch selection, bounded local repair and coarse-node recovery."""

import asyncio
import json
import logging
import re
from collections import Counter
from types import SimpleNamespace

from pageindex import IndexConfig

from openkb.pdf_navigation_requests import PDFContentError
from openkb.pdf_navigation_runtime import LegacyPDF


def runtime(respond, count=5, text_tokens=10):
    pages = SimpleNamespace(values=[(f"Page {i}", text_tokens) for i in range(1, count + 1)])
    pages.numbers = lambda text: sorted({int(n) for n in re.findall(r"physical_index_(\d+)", text)})
    calls = []

    def call(scope, prompt, history):
        calls.append((scope, prompt))
        return json.dumps(respond(scope[0], prompt, calls))

    requests = SimpleNamespace(call=call, settings={"model": "openai/offline-test"})
    return LegacyPDF(pages, requests), calls


def rows():
    return [
        {"structure": str(i), "title": title, "page": i, "physical_index": i + 1}
        for i, title in enumerate("ABCD", 1)
    ]


def test_bad_toc_locations_try_all_three_original_modes():
    def respond(name, prompt, calls):
        if name == "toc_transformer":
            return {"table_of_contents": rows()}
        if name == "check_if_toc_transformation_is_complete":
            return {"completed": "yes"}
        if name in {"toc_index_extractor", "add_page_number_to_toc", "generate_toc_init"}:
            return rows()
        if name == "check_title_appearance":
            checked = sum(scope[0] == name for scope, _ in calls)
            return {"answer": "yes" if checked > 8 else "no"}
        raise AssertionError(name)

    adapter, calls = runtime(respond)
    result = asyncio.run(
        adapter.functions["meta_processor"](
            adapter.pages.values,
            mode="process_toc_with_page_numbers",
            toc_content="A ... 1",
            toc_page_list=[0],
            opt=IndexConfig(model="openai/offline-test"),
            logger=logging.getLogger(__name__),
        )
    )
    counts = Counter(scope[0] for scope, _ in calls)
    assert (
        counts["toc_index_extractor"]
        == counts["add_page_number_to_toc"]
        == counts["generate_toc_init"]
        == 1
    )
    assert counts["check_title_appearance"] == 12
    assert [r["title"] for r in result] == list("ABCD")


def test_three_local_repair_rounds_preserve_other_titles():
    def respond(name, prompt, calls):
        if name == "generate_toc_init":
            return rows()
        if name == "check_title_appearance":
            return {"answer": "no" if "title is D" in prompt else "yes"}
        if name == "single_toc_item_index_fixer":
            return {"physical_index": "<physical_index_5>"}
        raise AssertionError(name)

    adapter, calls = runtime(respond)
    result = asyncio.run(
        adapter.functions["meta_processor"](
            adapter.pages.values,
            mode="process_no_toc",
            opt=IndexConfig(model="openai/offline-test"),
            logger=logging.getLogger(__name__),
        )
    )
    counts = Counter(scope[0] for scope, _ in calls)
    assert counts["single_toc_item_index_fixer"] == 3
    assert counts["check_title_appearance"] == 7
    assert [r["title"] for r in result] == list("ABCD")
    assert any(i["reason"] == "unconfirmed_titles" for i in adapter.issues)


def test_large_node_failure_preserves_coarse_node():
    def respond(name, prompt, calls):
        if name == "generate_toc_init":
            raise PDFContentError("invalid_pdf_structure")
        raise AssertionError(name)

    adapter, calls = runtime(respond, count=12, text_tokens=2000)
    node = {"title": "Coarse", "start_index": 1, "end_index": 12}
    result = asyncio.run(
        adapter.functions["process_large_node_recursively"](
            node,
            adapter.pages.values,
            IndexConfig(model="openai/offline-test"),
            logger=logging.getLogger(__name__),
        )
    )
    assert result == {"title": "Coarse", "start_index": 1, "end_index": 12}
    assert len(calls) == 1
    assert any(i["task"] == "recursive_expansion" for i in adapter.issues)


def test_page_groups_overlap_and_continuation_receives_accepted_structure():
    def respond(name, prompt, calls):
        assert name in {"generate_toc_init", "generate_toc_continue"}
        if name == "generate_toc_continue":
            assert "First section" in prompt
        return (
            [{"structure": "1", "title": "First section", "physical_index": 1}]
            if name == "generate_toc_init"
            else []
        )

    adapter, calls = runtime(respond, count=4)
    adapter.pages.values = [("word " * 8000, 8000)] * 4
    result = adapter.functions["process_no_toc"](
        adapter.pages.values, model="openai/offline-test", logger=logging.getLogger(__name__)
    )
    groups = [scope[2] for scope, _ in calls]
    assert len(groups) > 1
    assert all(a[-1] == b[0] for a, b in zip(groups, groups[1:]))
    assert result[0]["title"] == "First section"


def test_same_page_parent_and_child_still_visit_large_grandchild():
    def respond(name, prompt, calls):
        assert name == "generate_toc_init"
        raise PDFContentError("invalid_pdf_structure")

    adapter, calls = runtime(respond, count=13, text_tokens=2000)
    leaf = {"title": "Long branch", "start_index": 2, "end_index": 13}
    child = {"title": "Subheading", "start_index": 1, "end_index": 1, "nodes": [leaf]}
    parent = {"title": "Heading", "start_index": 1, "end_index": 1, "nodes": [child]}
    asyncio.run(
        adapter.functions["process_large_node_recursively"](
            parent,
            adapter.pages.values,
            IndexConfig(model="openai/offline-test"),
            logger=logging.getLogger(__name__),
        )
    )
    assert len(calls) == 1  # Small shared-page ancestors must not suppress expansion.
    assert all(issue["reason"] != "no_page_progress" for issue in adapter.issues)
    assert parent["nodes"][0]["nodes"][0] == leaf


def test_pdf_plan_uses_only_legacy_selection_rules():
    from openkb.agent.document_protocol import plan_messages

    common = dict(
        evidence={
            "blocks": [],
            "source_id": "a" * 32,
            "version_id": "b" * 64,
            "parse_id": "c" * 64,
        },
        carry_s={},
        target_t={},
        navigation_hints=[],
        catalog_window="",
        entity_types=["product"],
        schema="",
    )
    pdf = json.loads(
        plan_messages(**common, planning_context={"navigation_style": "legacy_pdf"})[-1]["content"]
    )
    generic = json.loads(plan_messages(**common)[-1]["content"])
    assert "supplied KB stage" in pdf["task_rules"]
    assert "cover a complete\ntask" not in pdf["task_rules"]
    assert pdf["task_rules"] == generic["task_rules"]
    assert (
        "without frontmatter, code fences or a page plan"
        in json.loads(
            plan_messages(
                **common, subtask="overview", planning_context={"navigation_style": "legacy_pdf"}
            )[-1]["content"]
        )["task_rules"]
    )
