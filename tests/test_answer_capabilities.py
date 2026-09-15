"""The main answer agent receives the configured image capability without a verifier."""

import json

import pytest

from openkb.application.conversations import ask_question, continue_conversation
from openkb.locks import atomic_write_text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [ask_question, continue_conversation])
@pytest.mark.parametrize("enabled", [False, True])
async def test_image_capability_reaches_answer_agent(kb_dir, model_service, operation, enabled):
    from openkb.config import load_config, save_config

    config_path = kb_dir / ".openkb/config.yaml"
    config = load_config(config_path)
    config["image_understanding"] = {"enabled": enabled}
    save_config(config_path, config)
    target = "sources/snapshots/v-p.md#block-row"
    source = f"Left: return path. [Original]({target})"
    atomic_write_text(kb_dir / "wiki/sources/paths.md", source)
    state = "enabled" if enabled else "disabled"
    answer = f"Left: return path. [Original]({target})\n\nImage understanding is {state}."

    def chat(body):
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        assert ("get_image" in names) is enabled
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": answer}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "sources/paths.md"}),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    result = await operation(kb_dir, "Read the path and state your image capability.")
    assert result.status == "completed", result
    assert result.answer == answer
    assert len(model_service) == 2
