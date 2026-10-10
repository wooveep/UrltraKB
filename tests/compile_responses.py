"""External model fixtures for the declared summary protocol, not semantic scoring."""

import json


def summary_reply(messages, reply):
    if isinstance(reply, dict) and "status" in reply:
        return reply  # Deliberate invalid protocol fixtures must stay invalid.
    if (
        isinstance(reply, dict)
        and "content" in reply
        and "Write a summary page for this document" in str(messages[-1].get("content"))
    ):
        return {"description": reply.get("description", "Fixture"), "content": reply["content"]}
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
