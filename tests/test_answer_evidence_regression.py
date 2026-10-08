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


@pytest.mark.asyncio
@pytest.mark.parametrize("slides", [False, True])
async def test_actual_tool_nullable_part_contract(kb_dir, slides):
    source(kb_dir, slides=slides)
    agent = agent_for(kb_dir)
    tool = next(tool for tool in agent.tools if tool.name == "get_page_content")
    schema = tool.params_json_schema["properties"]["part"]["anyOf"]
    assert {item["type"] for item in schema} == {"string", "null"}
    assert next(item["enum"] for item in schema if item["type"] == "string") == ["body", "notes"]
    full = await invoke(agent, tool.name, doc_name="manual", pages="1", view_id="", part=None)
    assert "Physical page 1" in full
    body = await invoke(agent, tool.name, doc_name="manual", pages="1", view_id="", part="body")
    notes = await invoke(agent, tool.name, doc_name="manual", pages="1", view_id="", part="notes")
    if slides:
        assert "Visible body" in body and "Speaker notes" not in body
        assert "Speaker notes" in notes and "Visible body" not in notes
    else:
        assert "part=null" in body and "part=null" in notes
    from openkb.agent.tools import get_wiki_page_content

    assert "Physical page 1" in get_wiki_page_content("manual", "1", str(kb_dir / "wiki"), "")


@pytest.mark.asyncio
async def test_selected_page_attaches_its_actual_image(kb_dir):
    import pymupdf
    from agents import ToolOutputImage, ToolOutputText

    path = "sources/diagram.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    pix.clear_with(128)
    (kb_dir / "wiki" / path).write_bytes(pix.tobytes("png"))
    source(kb_dir, images=[path])
    result = await invoke(
        agent_for(kb_dir), "get_page_content", doc_name="manual", pages="1", part=None
    )
    assert isinstance(result, list)
    assert any(isinstance(item, ToolOutputImage) for item in result)
    assert any(path in item.text for item in result if isinstance(item, ToolOutputText))


def test_answer_outcome_survives_runtime_receipt_readback():
    from openkb.runtime.records import UnitResult

    result = UnitResult("completed", answer_outcome="scope_unresolved")
    # JSON is the actual persisted receipt boundary.
    assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result
