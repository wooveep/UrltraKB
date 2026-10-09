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
    assert metadata["status"] == "pixels_delivered" and metadata["page"] == 1
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    images = json.loads(first.split("\nImage coverage: ")[1])["occurrences"]
    assert [i["status"] for i in images] == ["pixels_delivered", "unread"]
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
