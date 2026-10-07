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


@pytest.mark.asyncio
async def test_rule_cannot_borrow_condition_from_another_subject(kb_dir):
    source(kb_dir)
    agent = agent_for(kb_dir)
    await invoke(agent, "get_page_content", doc_name="manual", pages="1", part=None)
    arguments = {
        "subject": "Compute nodes",
        "setting": "management VIP",
        "quote": "Compute nodes use management VIP after joining the cluster.",
        "locator": "sources/manual.json pages=1 part=None",
    }
    bad = await invoke(
        agent, "record_source_fact", **arguments, condition="when external NTP is unset"
    )
    assert "Do not transfer conditions" in bad
    good = json.loads(
        await invoke(
            agent, "record_source_fact", **arguments, condition="after joining the cluster"
        )
    )
    assert good["subject"] == "Compute nodes" and good["condition"] == "after joining the cluster"


def test_filename_version_does_not_authorize_a_version_claim(kb_dir):
    from openkb.agent.query_evidence import evidence_answer

    source(kb_dir)
    answer = evidence_answer("Platform V9.3.1 requires these fields.", selection(kb_dir))
    assert "未通过证据检查" in answer
    assert "requires these fields" not in answer
    verified = evidence_answer(
        "Platform V9.3.1 requires these fields.", selection(kb_dir, ("9.3.1",))
    )
    assert "requires these fields" in verified
    filename = "Source: manual-V9.3.1.pdf；适用版本未确认。"
    assert evidence_answer(filename, selection(kb_dir)).startswith(filename)


def test_filename_hint_explanation_is_not_a_positive_version_claim(kb_dir):
    from openkb.agent.query_evidence import evidence_answer

    source(kb_dir)
    answer = (
        "知识库未提供已验证的适用产品与版本信息；文件名中的版本串（V9.4.0 等）"
        "仅为线索，不能据此断言版本适用性。"
    )
    assert evidence_answer(answer, selection(kb_dir)).startswith(answer)
    # A disclaimer in another sentence must not bless a positive assertion.
    assert "未通过证据检查" in evidence_answer(
        answer + "但平台 V9.5.0 必须采用此设置。", selection(kb_dir)
    )


def test_encoded_quote_recovery_requires_a_verbatim_original_match():
    from openkb.agent.answer_evidence import original_quote

    assert original_quote("NTP\\nserver", "NTP\nserver") == "NTP\nserver"
    assert original_quote("NTP server", "NTP\nserver") is None
    assert original_quote("a\\nb", "a\\nb") == "a\\nb"


def test_answer_outcome_survives_runtime_receipt_readback():
    from openkb.runtime.records import UnitResult

    result = UnitResult("completed", answer_outcome="scope_unresolved")
    # JSON is the actual persisted receipt boundary.
    assert UnitResult.from_summary(json.loads(json.dumps(result.summary()))) == result


@pytest.mark.asyncio
async def test_scope_unresolved_is_a_business_outcome_without_model(kb_dir, monkeypatch):
    from agents import Agent

    from openkb.agent.query import iter_agent_response_events

    monkeypatch.setattr(
        "openkb.agent.query.Runner.run_streamed", lambda *a, **k: pytest.fail("model called")
    )
    events = [
        event
        async for event in iter_agent_response_events(
            Agent(name="fixture"),
            "Unknown product?",
            selection=QuerySelection(kb_dir, (), ("Unconfirmed product alias",)),
        )
    ]
    assert events[-1]["data"]["answer_outcome"] == "scope_unresolved"
    assert "未生成知识答案" in events[-1]["data"]["answer"]
