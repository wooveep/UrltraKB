"""Shared model-I/O fixture for frozen block integration tests."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def wide_short_prompt(monkeypatch):
    """Make the first short request require segmentation at its message seam.

    Native locators need usable request capacity, so reader tests cannot force
    this route with a one-token model limit. Keep the later planning schema
    usable too: an oversized shared schema cannot succeed in either route.
    """
    from openkb.agent import compiler

    original = compiler.short_document_messages

    def messages(*args, **kwargs):
        return original(*args, **kwargs) + [
            {"role": "user", "content": "Full-document test instruction.\n" * 2000}
        ]

    monkeypatch.setattr(compiler, "short_document_messages", messages)


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
        from compile_responses import summary_reply

        response = summary_reply(kwargs["messages"], response)
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
