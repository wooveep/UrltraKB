"""Exercise the actual SDK tool schemas and frozen evidence readers."""

import json

import pytest

from openkb.application.query_views import QuerySelection, QueryView
from openkb.knowledge_scope import legacy_scope
from openkb.state import HashRegistry


def selection(kb_dir, versions=()):
    wiki = kb_dir / "wiki"
    files = {
        p.relative_to(wiki).as_posix(): HashRegistry.hash_file(p)
        for p in wiki.rglob("*")
        if p.is_file()
    }
    return QuerySelection(
        kb_dir, (QueryView(legacy_scope(kb_dir), "Platform", versions, None, (), files, 0),)
    )


def agent_for(kb_dir):
    from agents import Agent

    from openkb.agent.query_evidence import restrict_query_agent

    return restrict_query_agent(Agent(name="fixture", instructions=""), selection(kb_dir))


async def invoke(agent, name, **arguments):
    from agents.tool_context import ToolContext

    tool = next(tool for tool in agent.tools if tool.name == name)
    payload = json.dumps(arguments)
    return await tool.on_invoke_tool(
        ToolContext(context=None, tool_name=name, tool_call_id="fixture", tool_arguments=payload),
        payload,
    )


def source(kb_dir, *, slides=False, images=()):
    parts = {"body": "Visible body", "notes": "Speaker notes"}
    data = [
        {
            "page": 1,
            "unit_kind": "page",
            "content": "Visible body\nSpeaker notes"
            if slides
            else "Compute nodes use management VIP after joining the cluster.",
            "images": [{"path": image} for image in images],
            **({"parts": parts} if slides else {}),
        }
    ]
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps(data))


def test_bound_reader_schemas_only_accept_business_arguments(kb_dir):
    expected = {
        "search_knowledge": {"query"},
        "read_file": {"path"},
        "get_page_content": {"doc_name", "pages"},
        "get_text_content": {"doc_name", "chars", "offset"},
        "get_block_content": {"doc_name", "blocks", "offset"},
        "get_cell_content": {"doc_name", "cells", "offset"},
        "get_image": {"image_path"},
        "search_originals": {"terms", "doc_name"},
    }
    for tool in agent_for(kb_dir).tools:
        assert set(tool.params_json_schema["properties"]) == expected[tool.name]


@pytest.mark.asyncio
async def test_search_distinguishes_unknown_document_from_no_phrase_match(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    empty = await invoke(agent, "search_originals", terms=["unmentioned"], doc_name="manual")
    assert json.loads(empty)["status"] == "no_matches"
    unknown = await invoke(agent, "search_originals", terms=["VIP"], doc_name="missing")
    assert "Document not found" in unknown
    assert "index.md" in unknown


@pytest.mark.asyncio
async def test_first_catalog_read_uses_the_frozen_source_bytes(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    catalog = await invoke(agent, "read_file", path="index.md")
    assert '"doc_name": "manual"' in catalog
    assert '"pages": "1-1"' in catalog
    (kb_dir / "wiki/sources/manual.json").write_text("[]")
    changed = await invoke(agent, "read_file", path="index.md")
    assert "changed after selection" in changed
    assert "start a new question" in changed


def test_application_rejects_ambiguous_reading_collections(kb_dir):
    view = selection(kb_dir).views[0]
    with pytest.raises(ValueError, match="Multiple source collections"):
        QuerySelection(kb_dir, (view, view))


@pytest.mark.asyncio
async def test_ordinary_pages_require_only_the_native_page_selector(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    tool = next(tool for tool in agent.tools if tool.name == "get_page_content")
    assert set(tool.params_json_schema["required"]) == {"doc_name", "pages"}
    assert "Physical page 1" in await invoke(agent, tool.name, doc_name="manual", pages="1")


@pytest.mark.asyncio
async def test_selected_page_returns_image_location_for_explicit_read(kb_dir):
    import pymupdf
    from agents import ToolOutputImage

    path = "sources/diagram.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    pix.clear_with(128)
    (kb_dir / "wiki" / path).write_bytes(pix.tobytes("png"))
    source(kb_dir, images=[path])
    agent = agent_for(kb_dir)
    result = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert isinstance(result, str)
    assert path in result and "not read" in result and "get_image" in result
    assert "data:image" not in result
    image = await invoke(agent, "get_image", image_path=path)
    assert isinstance(image, ToolOutputImage)
    assert image.image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_image_changes_have_actionable_feedback(kb_dir):
    path = "sources/images/diagram.png"
    image = kb_dir / "wiki" / path
    image.write_bytes(b"original")
    agent = agent_for(kb_dir)
    image.write_bytes(b"changed")
    result = await invoke(agent, "get_image", image_path=path)
    assert json.loads(result)["reason_code"] == "stale_snapshot"
    assert "changed after selection" in json.loads(result)["message"]
    image.unlink()
    missing = await invoke(agent, "get_image", image_path=path)
    assert json.loads(missing)["reason_code"] == "content_not_found"
    assert "missing" in json.loads(missing)["message"]


@pytest.mark.asyncio
async def test_derived_page_read_points_to_original_without_certifying_its_claims(kb_dir):
    source(kb_dir)
    (kb_dir / "wiki/summaries/manual.md").write_text(
        "---\ntype: Summary\nfull_text: sources/manual.json\n---\nGenerated summary"
    )
    (kb_dir / "wiki/concepts/rules.md").write_text(
        "---\ntype: Concept\nsources: [summaries/manual.md]\n---\nUnsupported guarantee"
    )
    result = await invoke(agent_for(kb_dir), "read_file", path="concepts/rules.md")
    assert '"content_kind": "derived_concept"' in result
    assert '"doc_name": "manual"' in result
    assert '"reader": "get_page_content"' in result
    assert "not factual verification" in result


def test_answer_outcome_survives_runtime_receipt_readback():
    from openkb.runtime.records import UnitResult

    result = UnitResult("completed", answer_outcome="scope_unresolved")
    # JSON is the actual persisted receipt boundary.
    assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
