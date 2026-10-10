"""External model fixtures for the declared summary protocol, not semantic scoring."""

import json


def summary_reply(messages, reply):
    for message in messages:
        text = message.get("content")
        if not isinstance(text, str) or not text.startswith("Original document: "):
            continue
        batch = json.loads(text.split("\n", 1)[1])
        if "output_sections" not in batch or not isinstance(reply, dict) or "content" not in reply:
            return reply
        body = reply["content"]
        return {
            "description": reply.get("description", "Fixture"),
            "sections": [
                {
                    "id": section["id"],
                    "content": (
                        batch["content"][section["start"] : section["end"]]
                        if body == batch["content"] or index
                        else body
                    ),
                }
                for index, section in enumerate(batch["output_sections"])
            ],
        }
    return reply
