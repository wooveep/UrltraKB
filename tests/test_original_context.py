"""Original reader boundaries preserve scope and individual image deliveries."""

import json

import pytest
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
async def test_each_retained_occurrence_has_its_own_location_without_pixel_actions(kb_dir):
    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# First\nNative text", "# Second\nNative text"], ["sources/figure.png"])
    agent = agent_for(kb_dir)
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    second = await invoke(agent, "get_page_content", doc_name="manual", pages="2")
    a = json.loads(first.split("\nImage coverage: ")[1])["occurrences"][0]
    b = json.loads(second.split("\nImage coverage: ")[1])["occurrences"][0]
    assert a["occurrence_id"] != b["occurrence_id"]
    assert a["path"] == b["path"]
    assert a["page"] == 1 and b["page"] == 2
    assert a["status"] == b["status"] == "unread"
    assert "reading_action" not in a and "reading_action" not in b
    assert "get_image" not in {tool.name for tool in agent.tools}


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
