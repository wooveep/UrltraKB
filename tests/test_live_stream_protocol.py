"""Failure-only SSE inputs offline; successful frames come from this run's Flash."""

import hashlib
import json
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from live_stream import DeepSeekStream
from llm_test_budget import TestRunBudget


def failed_stream(tmp_path, values):
    """Attach invalid/empty protocol input to the decoder, without a model port."""
    budget = TestRunBudget(tmp_path / "local-reservation.json")
    identifier = budget.reserve(input_tokens=32, output_tokens=8, fingerprint="invalid-sse")
    cached = tmp_path / "cache.json"
    owner = SimpleNamespace(
        budget=budget,
        receipts=[],
        _save_response=lambda key, value: cached.write_text(json.dumps(value)),
    )
    released = []
    lease = ExitStack()
    lease.callback(lambda: released.append(True))
    stream = DeepSeekStream(owner, {}, "invalid-sse", identifier, True, {}, 1, lease=lease)

    async def lines():
        for value in values:
            yield value

    stream._lines = lines()
    return stream, owner, cached, released


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("termination", [[], ["data: [DONE]"]], ids=["eof", "done"])
async def test_empty_stream_never_refunds_or_caches_without_a_usage_receipt(tmp_path, termination):
    stream, owner, cached, released = failed_stream(tmp_path, termination)
    assert [chunk async for chunk in stream] == []
    assert owner.receipts == [] and not cached.exists()
    report = owner.budget.report()
    assert report["actual_cny"] == "0" and report["unknown_requests"] == 1
    assert report["provider_requests"] == 0 and float(report["exposure_cny"]) > 0
    assert released == [True]
    await stream.aclose()
    assert released == [True]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("close_first", [False, True], ids=["iterate", "early-close"])
async def test_done_terminates_before_any_trailing_transport_junk(tmp_path, close_first):
    stream, owner, cached, released = failed_stream(
        tmp_path, ["data: [DONE]", "data: this-is-trailing-junk"]
    )
    if close_first:
        await stream.aclose()
    else:
        assert [chunk async for chunk in stream] == []
    assert not cached.exists() and owner.receipts == [] and released == [True]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_corrupt_frame_preserves_the_first_decode_error_and_releases_the_lease(tmp_path):
    stream, owner, cached, released = failed_stream(
        tmp_path, ["data: first-corrupt-frame", "data: second-corrupt-frame"]
    )
    with pytest.raises(json.JSONDecodeError) as caught:
        await anext(stream)
    assert caught.value.doc == "first-corrupt-frame"
    assert not cached.exists() and owner.receipts == [] and released == [True]
    assert owner.budget.report()["unknown_requests"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["null", "[]", "7", '{"error":{"message":"stream failed"}}'])
async def test_non_chunk_json_is_rejected_without_delivering_a_success(tmp_path, value):
    stream, owner, cached, released = failed_stream(tmp_path, [f"data: {value}"])
    with pytest.raises(ValueError, match="SSE|chunk|choices"):
        await anext(stream)
    assert not cached.exists() and owner.receipts == [] and released == [True]
    assert owner.budget.report()["unknown_requests"] == 1


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_stream_early_close_drains_and_reuses_only_complete_original_frames(live_llm):
    request = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Return exactly: Orion has 17 credits."}],
        "max_tokens": 64,
        "temperature": 0,
        "stream": True,
    }
    before = live_llm.budget.report()["provider_requests"]
    stream = await live_llm.remote_acompletion(**request)
    assert isinstance(stream, DeepSeekStream)
    first = await anext(stream)
    assert first.id and first.choices
    await stream.aclose()
    cached = live_llm._cached_response(stream.fingerprint)
    assert cached and cached["stream_chunks"]
    assert cached["stream_chunks"] == stream._chunks
    assert len(live_llm.receipts) >= 1
    usage = live_llm.receipts[-1]["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert any(c.get("usage") == usage for c in cached["stream_chunks"])
    reused = await live_llm.remote_acompletion(**request)
    received = [c async for c in reused]
    await reused.aclose()
    answer = "".join(choice.delta.content or "" for chunk in received for choice in chunk.choices)
    assert answer.strip() == "Orion has 17 credits."
    assert any(choice.finish_reason == "stop" for c in received for choice in c.choices)
    assert live_llm.budget.report()["provider_requests"] == before + 1
    # Faults are introduced at the protocol boundary using the unmodified real
    # frames just captured; no synthetic successful model response is provided.
    from live_sse import StreamCapture

    frames = cached["stream_chunks"]
    complete = StreamCapture()
    for chunk in frames:
        complete.accept(json.dumps(chunk))
    complete.accept("[DONE]")
    assert complete.cacheable and complete.chunks == frames
    assert complete.usage == usage
    for final_marker in (None, "[DONE]"):
        missing_usage = StreamCapture()
        for chunk in frames:
            if not isinstance(chunk.get("usage"), dict):
                missing_usage.accept(json.dumps(chunk))
        if final_marker:
            missing_usage.accept(final_marker)
        assert not missing_usage.cacheable
    interrupted = StreamCapture()
    for chunk in frames:
        interrupted.accept(json.dumps(chunk))
    assert interrupted.usage == usage and not interrupted.cacheable  # EOF before DONE
    with pytest.raises(json.JSONDecodeError):
        interrupted.accept("corrupt-after-the-real-receipt")
    assert not interrupted.cacheable and interrupted.usage == usage
    # Receipt location need not coincide with a text delta, or cause double
    # settlement; accepting original metadata first must still await completion.
    receipt_first = StreamCapture()
    for chunk in frames:
        if isinstance(chunk.get("usage"), dict):
            receipt_first.accept(json.dumps(chunk))
    assert not receipt_first.cacheable
    for chunk in frames:
        if not isinstance(chunk.get("usage"), dict):
            receipt_first.accept(json.dumps(chunk))
    receipt_first.accept("[DONE]")
    assert receipt_first.cacheable and receipt_first.usage == usage


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_truncated_frames_do_not_cache_a_stream_with_a_settled_receipt(
    live_llm, tmp_path
):
    """Exercise the actual decoder using transport faults on current-run frames."""
    request = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Return exactly: Orion has 17 credits."}],
        "max_tokens": 64,
        "temperature": 0,
        "stream": True,
    }
    payload, _ = live_llm._payload(request)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    original = await live_llm.remote_acompletion(**request)
    async for _ in original:
        pass
    await original.aclose()
    frames = live_llm._cached_response(fingerprint)["stream_chunks"]
    ledger = json.loads(live_llm.budget.path.read_text())
    entry = next(e for e in ledger["entries"] if e["fingerprint"] == fingerprint)
    assert entry["sent"] and entry["state"] == "settled"
    report_before = live_llm.budget.report()
    original_lines = ["data: " + json.dumps(chunk) for chunk in frames]
    cases = {
        "eof-after-usage": original_lines,
        "corruption-after-usage": [*original_lines, "data: corrupt-after-usage"],
        "done-without-usage": [
            *("data: " + json.dumps(c) for c in frames if not isinstance(c.get("usage"), dict)),
            "data: [DONE]",
        ],
    }
    for name, values in cases.items():
        cached = tmp_path / f"{name}.json"
        owner = SimpleNamespace(
            budget=live_llm.budget,
            receipts=[],
            _save_response=lambda key, value, path=cached: path.write_text(json.dumps(value)),
        )
        released = []
        lease = ExitStack()
        lease.callback(lambda: released.append(True))
        stream = DeepSeekStream(
            owner,
            payload,
            fingerprint,
            entry["id"],
            entry["period"] == "peak",
            {},
            1,
            lease=lease,
        )

        async def lines(selected=values):
            for value in selected:
                yield value

        stream._lines = lines()
        if name == "corruption-after-usage":
            with pytest.raises(json.JSONDecodeError):
                async for _ in stream:
                    pass
        else:
            async for _ in stream:
                pass
        assert not cached.exists(), name
        assert released == [True], name
        await stream.aclose()
        assert released == [True], name
    # Re-decoding is not a model send or a new charge. The original settled ID
    # keeps its known fee even when its captured transport is cut or corrupted.
    assert live_llm.budget.report() == report_before
