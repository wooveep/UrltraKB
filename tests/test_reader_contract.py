"""Final SDK tools follow frozen native source capabilities."""

import json

import pytest
from test_answer_evidence_regression import agent_for, invoke, selection, source


def test_no_native_source_tools_are_exposed_without_that_capability(kb_dir):
    names = {tool.name for tool in agent_for(kb_dir).tools}
    assert names == {"read_file", "search_knowledge"}


@pytest.mark.asyncio
@pytest.mark.parametrize("slides", [False, True])
async def test_final_page_and_slide_schemas_have_no_nullable_part(kb_dir, slides):
    source(kb_dir, slides=slides)
    agent = agent_for(kb_dir)
    names = {tool.name for tool in agent.tools}
    name = "get_slide_content" if slides else "get_page_content"
    assert ("get_page_content" not in names) if slides else ("get_slide_content" not in names)
    tool = next(tool for tool in agent.tools if tool.name == name)
    assert set(tool.params_json_schema["properties"]) == (
        {"doc_name", "slides", "part", "offset"} if slides else {"doc_name", "pages", "offset"}
    )
    args = (
        {"doc_name": "manual", "slides": "1", "part": "all"}
        if slides
        else {"doc_name": "manual", "pages": "1"}
    )
    assert "Physical page 1" in await invoke(agent, name, **args)
    if slides:
        assert tool.params_json_schema["properties"]["part"]["enum"] == ["all", "body", "notes"]
        assert "Speaker notes" in await invoke(agent, name, **{**args, "part": "notes"})
        bad = await invoke(agent, name, **{**args, "part": "null"})
        assert json.loads(bad)["reason_code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_wrong_reader_and_outside_range_offer_executable_actions(kb_dir):
    source(kb_dir)
    (kb_dir / "wiki/sources/pages.json").write_bytes(
        (kb_dir / "wiki/sources/manual.json").read_bytes()
    )
    source(kb_dir, slides=True)
    agent = agent_for(kb_dir)
    wrong = json.loads(await invoke(agent, "get_page_content", doc_name="manual", pages="1"))
    assert wrong["reason_code"] == "tool_not_applicable"
    action = wrong["reading_action"]
    assert action["tool"] == "get_slide_content"
    assert "Visible body" in await invoke(agent, action["tool"], **action["arguments"])
    outside = json.loads(await invoke(agent, "get_page_content", doc_name="pages", pages="20"))
    assert outside["reason_code"] == "range_out_of_bounds"
    assert outside["reading_action"]["arguments"]["pages"] == "1"


def test_final_query_and_skill_prompts_match_their_tools(kb_dir, tmp_path):
    from ultrakb.agent.query import build_query_agent
    from ultrakb.agent.query_evidence import restrict_query_agent
    from ultrakb.skill.creator import build_skill_create_agent

    source(kb_dir)
    query = restrict_query_agent(
        build_query_agent(str(kb_dir / "wiki"), "gpt-4o"), selection(kb_dir)
    )
    skill = restrict_query_agent(
        build_skill_create_agent(
            wiki_root=str(kb_dir / "wiki"),
            skill_root=str(tmp_path / "skill"),
            skill_name="gardening",
            intent="Prune roses",
            model="gpt-4o",
        ),
        selection(kb_dir),
    )
    for agent in (query, skill):
        assert "doc_type: short" not in agent.instructions
        assert "doc_type: pageindex" not in agent.instructions
        for obsolete in ("list_wiki_dir", "read_wiki_file", "query_wiki", "get_cell_content"):
            assert obsolete not in agent.instructions
    assert {"write_skill_file", "done"} <= {tool.name for tool in skill.tools}
