"""Answer verification sees frozen execution capability, separate from source facts."""

import json

import pytest

from openkb.application.conversations import ask_question, continue_conversation
from openkb.locks import atomic_write_text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [ask_question, continue_conversation])
@pytest.mark.parametrize("enabled", [False, True])
async def test_image_capability_reaches_answer_review(kb_dir, model_service, operation, enabled):
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
    reviews = []

    def chat(body):
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

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        capability = next(
            (o for o in payload["observations"] if o.get("name") == "execution_capabilities"), None
        )
        assert capability is not None
        assert capability["output"] == {"image_understanding_enabled": enabled}
        units = []
        for unit in payload["units"]:
            limited = "Image understanding" in unit["text"]
            units.append(
                {
                    "id": unit["id"],
                    "verdict": "supported",
                    "support": [
                        {
                            "observation": capability["id"] if limited else "o1",
                            "quote": json.dumps(capability["output"]) if limited else source,
                        }
                    ],
                }
            )
        return {
            "role": "assistant",
            "content": json.dumps({"verdict": "supported", "units": units, "issues": []}),
        }

    model_service.chat_response = chat
    model_service.answer_review_response = review
    model_service.chat_without_tools = True
    result = await operation(kb_dir, "Read the path and state your image capability.")
    assert result.status == "completed", result
    assert result.answer == answer
    assert len(reviews) == 1
