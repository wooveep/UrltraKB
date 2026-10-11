"""The actual model boundary accepts text evidence and rejects image payloads."""

import json

import pytest
from agents import RunConfig, Runner
from test_llm_execution import executor_for
from test_managed_agent import agent_for
from test_vendor_sdk import response

from openkb.llm_execution import ModelRequest

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.asyncio
async def test_image_messages_never_reach_the_model(model_service, sdk):
    executor = executor_for(model_service)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image" if sdk else "image_url",
                    "image_url": "data:image/png;base64,AAAA"
                    if sdk
                    else {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
    model_service.replies.append((200, response()))
    with pytest.raises(ValueError, match="text-only"):
        if sdk:
            await Runner.run(
                agent_for(executor), messages, run_config=RunConfig(tracing_disabled=True)
            )
        else:
            await executor.acomplete(
                "compile", ModelRequest(messages, operation="compile", stage="summary")
            )
    assert not model_service.requests
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_text_references_and_literal_code_survive_real_sdk(model_service):
    content = (
        '![Original, page 2](../sources/images/figure.png)\n```json\n{"type":"image_url"}\n```'
    )
    model_service.replies.append((200, response()))
    await Runner.run(
        agent_for(executor_for(model_service)), content, run_config=RunConfig(tracing_disabled=True)
    )
    assert content in json.dumps(model_service.requests[0], ensure_ascii=False).replace(
        "\\n", "\n"
    ).replace('\\"', '"')


@pytest.mark.asyncio
async def test_reading_tools_list_every_occurrence_without_offering_pixels(kb_dir):
    from test_original_context import agent_for as reading_agent
    from test_original_context import invoke, pages

    pages(kb_dir, ["Native explanation."], [f"sources/figure-{i}.png" for i in range(9)])
    agent = reading_agent(kb_dir)
    assert "get_image" not in {tool.name for tool in agent.tools}
    content = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    coverage = json.loads(content.split("\nImage coverage: ")[1])
    assert len(coverage["occurrences"]) == 9
    assert "get_image" not in json.dumps(coverage)
    assert "Native explanation." in content


@pytest.mark.asyncio
async def test_old_image_tool_history_is_projected_as_text_without_mutating_archive(model_service):
    from copy import deepcopy

    from openkb.agent.chat_session import sanitize_history

    history = [
        {"role": "user", "content": "Read the figure."},
        {
            "type": "function_call",
            "name": "get_image",
            "call_id": "old-figure",
            "arguments": '{"image_path":"sources/old.png"}',
        },
        {
            "type": "function_call_output",
            "call_id": "old-figure",
            "output": [
                {"type": "input_image", "image_url": "https://example.org/old.png"},
            ],
        },
    ]
    before = deepcopy(history)
    model_service.replies.append((200, response()))
    await Runner.run(
        agent_for(executor_for(model_service)),
        sanitize_history(history),
        run_config=RunConfig(tracing_disabled=True),
    )
    sent = json.dumps(model_service.requests)
    assert "sources/old.png" in sent
    assert "input_image" not in sent and "image_url" not in sent
    assert "not interpreted" in sent
    assert history == before
