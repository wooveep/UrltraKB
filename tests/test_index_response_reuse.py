"""One paid response per exact request, including concurrent callers."""

import asyncio

import litellm
import pytest

from ultrakb.index_llm import PageIndexLLM
from ultrakb.index_stages import IndexStages
from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings


def adapter(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"retained fixture")
    executor = CompletionExecutor(
        RoleBindings("openai/fixture", LlmCredentialBundle(), model_table={})
    )
    client = PageIndexLLM(executor)
    client.checkpoints = IndexStages(tmp_path, source, identity={"fixture": True})
    return client


def response():
    return litellm.ModelResponse(
        choices=[
            {
                "message": {"role": "assistant", "content": '{"answer":"yes"}'},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


def test_response_reuse_is_not_limited_to_one_read_per_execution(tmp_path, monkeypatch):
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return response()

    monkeypatch.setattr(litellm, "completion", send)
    client = adapter(tmp_path)
    prompt = [{"role": "user", "content": "The exact retained request"}]
    first = client.complete(prompt, stage="verification")
    assert client.complete(prompt, stage="verification") == first
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_the_in_flight_response(tmp_path, monkeypatch):
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.01)
        return response()

    monkeypatch.setattr(litellm, "acompletion", send)
    client = adapter(tmp_path)
    prompt = [{"role": "user", "content": "Same heading and source context"}]
    results = await asyncio.gather(
        *(client.acomplete(prompt, stage="verification") for _ in range(3))
    )
    assert results[0] == results[1] == results[2]
    assert len(calls) == 1


def test_delivered_response_is_reusable_by_a_new_adapter(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: (calls.append(1), response())[1])
    prompt = [{"role": "user", "content": "Persisted paid response"}]
    first = adapter(tmp_path).complete(prompt, stage="verification")
    assert adapter(tmp_path).complete(prompt, stage="verification") == first
    assert calls == [1]


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_cancel_the_shared_request(tmp_path, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(**kwargs):
        calls.append(1)
        entered.set()
        await finish.wait()
        return response()

    monkeypatch.setattr(litellm, "acompletion", send)
    client = adapter(tmp_path)
    prompt = [{"role": "user", "content": "Retain the owner"}]
    owner = asyncio.create_task(client.acomplete(prompt, stage="verification"))
    await entered.wait()
    waiter = asyncio.create_task(client.acomplete(prompt, stage="verification"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish.set()
    result = await owner
    assert await client.acomplete(prompt, stage="verification") == result
    assert calls == [1]


@pytest.mark.asyncio
async def test_sync_and_async_callers_share_one_request(tmp_path, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(**kwargs):
        calls.append("async")
        entered.set()
        await finish.wait()
        return response()

    monkeypatch.setattr(litellm, "acompletion", send)
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: pytest.fail("duplicate sync send"))
    client = adapter(tmp_path)
    prompt = [{"role": "user", "content": "Mixed callers"}]
    owner = asyncio.create_task(client.acomplete(prompt, stage="verification"))
    await entered.wait()
    waiter = asyncio.create_task(asyncio.to_thread(client.complete, prompt, stage="verification"))
    finish.set()
    assert await waiter == await owner
    assert calls == ["async"]


def test_an_unchanged_failed_request_is_not_sent_again(tmp_path, monkeypatch):
    calls = []

    def send(**kwargs):
        calls.append(1)
        raise ValueError("deterministic response failure")

    monkeypatch.setattr(litellm, "completion", send)
    client = adapter(tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError, match="deterministic"):
            client.complete([{"role": "user", "content": "unchanged"}], stage="verification")
    assert calls == [1]
