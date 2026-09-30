"""Shared model-I/O fixture for frozen block integration tests."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def block_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    requests = []

    def completion(**kwargs):
        prompt = "\n".join(str(m.get("content", "")) for m in kwargs["messages"])
        requests.append(prompt)
        if "CONTENT BLOCK STRUCTURE" in prompt:
            response = [
                {
                    "structure": "1",
                    "title": "A greeting",
                    "title_origin": "generated",
                    "physical_index": 1,
                    "anchor": {"unit": 1, "part": "body", "range": [0, 5], "excerpt": "hello"},
                }
            ]
        else:
            response = {
                "description": "Greeting",
                "content": "A greeting.",
                "create": [],
                "update": [],
                "related": [],
            }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    return requests
