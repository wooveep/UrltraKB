"""Original reader boundaries preserve scope and individual image deliveries."""

import json

import pytest
from agents import ToolOutputImage
from PIL import Image
from test_answer_evidence_regression import agent_for, invoke


def pages(kb_dir, contents, images=()):
    (kb_dir / "wiki/sources/manual.json").write_text(
        json.dumps(
            [
                {
                    "page": n,
                    "unit_kind": "page",
                    "content": body,
                    "images": [{"path": p} for p in images],
                }
                for n, body in enumerate(contents, 1)
            ]
        )
    )


@pytest.mark.asyncio
async def test_same_field_keeps_distinct_original_operations(kb_dir):
    pages(
        kb_dir,
        ["# Create orchard\ncapacity is optional.\n# Extend irrigation\ncapacity is required."],
    )
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert scopes[0]["headings"][-1]["text"] == "Create orchard"
    assert scopes[1]["headings"][-1]["text"] == "Extend irrigation"
    assert all(scope["status"] == "source_heading" for scope in scopes)
    assert all(hit["source_context"]["content_digest"] for hit in result["hits"])


@pytest.mark.asyncio
async def test_unknown_parent_is_explicit_and_adjacent_action_executes(kb_dir):
    pages(kb_dir, ["Unlabelled fragment", "Neighbor content"])
    agent = agent_for(kb_dir)
    result = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    context = json.loads(result.split("\nSource context: ")[1].split("\n")[0])
    assert context["regions"][0]["status"] == "unconfirmed"
    action = context["adjacent_actions"][-1]
    assert "Neighbor content" in await invoke(agent, action["tool"], **action["arguments"])


@pytest.mark.asyncio
async def test_each_occurrence_records_only_its_own_pixels(kb_dir):
    for name, color in (("blank.png", "white"), ("table.png", "black")):
        Image.new("RGB", (32, 32), color).save(kb_dir / "wiki/sources" / name)
    pages(
        kb_dir, ["# First\nFigure", "# Second\nFigure"], ["sources/blank.png", "sources/table.png"]
    )
    agent = agent_for(kb_dir)
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    images = json.loads(first.split("\nImage coverage: ")[1])["occurrences"]
    action = images[0]["reading_action"]
    delivered = await invoke(agent, action["tool"], **action["arguments"])
    assert isinstance(delivered[1], ToolOutputImage)
    import base64
    import hashlib

    from agents.items import ItemHelpers
    from openai.types.responses import ResponseFunctionToolCall

    outgoing = ItemHelpers.tool_call_output_item(
        ResponseFunctionToolCall(
            name="get_image", arguments="{}", call_id="image", type="function_call"
        ),
        delivered,
    )["output"]
    assert [part["type"] for part in outgoing] == ["input_text", "input_image"]
    pixels = base64.b64decode(outgoing[1]["image_url"].split(",", 1)[1])
    assert hashlib.sha256(pixels).hexdigest() == json.loads(outgoing[0]["text"])["content_digest"]
    metadata = json.loads(delivered[0].text)
    assert metadata["occurrence_id"] == images[0]["occurrence_id"]
    assert metadata["status"] == "delivery_pending" and metadata["page"] == 1
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    images = json.loads(first.split("\nImage coverage: ")[1])["occurrences"]
    assert [i["status"] for i in images] == ["delivery_pending", "unread"]
    second = await invoke(agent, "get_page_content", doc_name="manual", pages="2")
    images2 = json.loads(second.split("\nImage coverage: ")[1])["occurrences"]
    assert images2[0]["status"] == "unread"
    assert images2[0]["occurrence_id"] != images[0]["occurrence_id"]
    (kb_dir / "wiki/sources/table.png").unlink()
    action = images[1]["reading_action"]
    assert (
        json.loads(await invoke(agent, action["tool"], **action["arguments"]))["status"] == "error"
    )
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert json.loads(first.split("\nImage coverage: ")[1])["occurrences"][1]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("size,context_limit", [(4000, None), (512, 800)])
async def test_image_context_admission_precedes_delivery(kb_dir, size, context_limit):
    from types import SimpleNamespace

    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, RoleBindings

    Image.new("RGB", (size, size), "white").save(kb_dir / "wiki/sources/large.png")
    pages(kb_dir, ["# Figure\nImage"], ["sources/large.png"])
    executor = CompletionExecutor(RoleBindings("openai/gpt-4o-mini", LlmCredentialBundle()))
    if context_limit:
        executor.question_capacity = SimpleNamespace(
            input_limit=context_limit, payload_tokens=lambda payload: 100
        )
        executor.check_capacity_input({"input": "question"})
    with executor.activate():
        agent = agent_for(kb_dir)
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    occurrence = json.loads(content.split("\nImage coverage: ")[1])["occurrences"][0]
    output = json.loads(await invoke(agent, "get_image", image_path=occurrence["occurrence_id"]))
    assert output["reason_code"] == "image_capacity"
    executor.question_capacity = None  # Inspect coverage independently of exhausted context.
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert (
        json.loads(content.split("\nImage coverage: ")[1])["occurrences"][0]["status"] == "failed"
    )


def test_projected_native_spans_define_context_coverage():
    from openkb.original_context import original_context

    source = {
        "path": "sources/sheet.content.json",
        "source_revision_id": "revision",
        "content_digest": "digest",
        "unit_kind": "worksheet",
        "readers": {},
        "reading_action": {"tool": "read_file", "arguments": {"path": "index.md"}},
    }
    raw = {"text": "A1 unrelated\nA2 target\nA3 unrelated"}
    context = original_context(source, raw, {"cells": "A2"}, selected={"source_spans": [[13, 22]]})
    assert context["regions"][0]["char_range"] == [13, 22]
    assert "A1" not in context["regions"][0]["nearby_original"]
    assert original_context(source, raw, {"chars": "0:36"}, selected={})["regions"] == []


@pytest.mark.asyncio
async def test_image_admission_counts_other_tools_from_the_same_turn(kb_dir):
    from types import SimpleNamespace

    from openkb.config import LlmCredentialBundle
    from openkb.llm_execution import CompletionExecutor, RoleBindings

    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# Figure\nCONTEXT_LOAD"], ["sources/figure.png"])
    executor = CompletionExecutor(RoleBindings("openai/gpt-4o-mini", LlmCredentialBundle()))
    executor.question_capacity = SimpleNamespace(
        input_limit=2600,
        payload_tokens=lambda payload: 1200 if "CONTEXT_LOAD" in str(payload) else 400,
    )
    executor.check_capacity_input({"input": "question"})
    with executor.activate():
        agent = agent_for(kb_dir)
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert "CONTEXT_LOAD" in content
    occurrence = json.loads(content.split("\nImage coverage: ")[1])["occurrences"][0]
    output = await invoke(agent, "get_image", image_path=occurrence["occurrence_id"])
    assert isinstance(output, str) and json.loads(output)["reason_code"] == "image_capacity"
