"""Shared model-I/O fixture for frozen block integration tests."""

import pytest


@pytest.fixture
def wide_short_prompt(monkeypatch):
    """Make the first short request require segmentation at its message seam.

    Native locators need usable request capacity, so reader tests cannot force
    this route with a one-token model limit. Keep the later planning schema
    usable too: an oversized shared schema cannot succeed in either route.
    """
    from ultrakb.agent import compiler

    original = compiler.short_document_messages

    def messages(*args, **kwargs):
        return original(*args, **kwargs) + [
            {"role": "user", "content": "Full-document test instruction.\n" * 2000}
        ]

    monkeypatch.setattr(compiler, "short_document_messages", messages)


@pytest.fixture
def block_model(monkeypatch, live_llm):
    live_llm.install(monkeypatch)
    requests = []

    # Observe exactly what the compiler prepared without changing the reply.
    def completion(**kwargs):
        requests.append("\n".join(str(m.get("content", "")) for m in kwargs["messages"]))
        return live_llm.completion(**kwargs)

    async def acompletion(**kwargs):
        requests.append("\n".join(str(m.get("content", "")) for m in kwargs["messages"]))
        return await live_llm.acompletion(**kwargs)

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    return requests
