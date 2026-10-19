"""Real TCP disconnects through uvicorn and the shared application use cases.

Offline cases use a loopback HTTP error server. The live case uses real Flash
through the shared budgeted gateway. SDK gates only delay timing and preserve
the actual result, exception, and stream chunks.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import litellm
import pytest
import uvicorn
from error_transport import ErrorTransport
from fastapi import UploadFile
from native_usage_fixtures import read_requests

from ultrakb.config import load_config, save_config
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.lifecycle import exclusive_lifecycle
from ultrakb.locks import atomic_write_text, kb_ingest_lock


class _SocketServer:
    """Observe actual ASGI disconnects without changing messages or application work."""

    def __init__(self, app):
        self.disconnected = threading.Event()
        self.finished = threading.Event()
        self.frames = []
        ready = threading.Event()
        owner = self

        async def observe(scope, receive, send):
            if scope["type"] != "http" or scope["method"] != "POST":
                return await app(scope, receive, send)

            async def observed_receive():
                message = await receive()
                if message["type"] == "http.disconnect":
                    owner.disconnected.set()
                return message

            async def observed_send(message):
                if message["type"] == "http.response.body":
                    owner.frames.append(message.get("body", b"").decode())
                await send(message)

            try:
                return await app(scope, observed_receive, observed_send)
            finally:
                owner.finished.set()

        class Server(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.server = Server(
            uvicorn.Config(
                observe,
                loop="asyncio",
                log_level="critical",
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True
        )
        self.thread.start()
        assert ready.wait(5), "The real HTTP server did not start"

    def close(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.listener.close()
        assert not self.thread.is_alive(), "The HTTP server retained unfinished request work"


@pytest.fixture
def socket_api(kb_dir, tmp_path, monkeypatch):
    """No real credentials/profile or project .env are consulted by this adapter."""
    from ultrakb.api import create_app

    profile = tmp_path / "isolated-profile"
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_DIR", profile)
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_PATH", profile / "global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "local-lifecycle-token")
    monkeypatch.setattr("ultrakb.api.load_dotenv", lambda: False)
    monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
    server = _SocketServer(create_app())
    try:
        yield server
    finally:
        server.close()


def _start(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.iter_lines()
    assert next(lines) == "event: start"
    assert next(lines).startswith("data: ")
    assert next(lines) == ""
    return lines  # Keep the streaming iterator alive until the deliberate TCP close.


@pytest.mark.parametrize("endpoint", ["query", "chat"])
@pytest.mark.integration
def test_tcp_disconnect_holds_lease_until_real_sdk_cleanup_and_preserves_no_half_turn(
    kb_dir, socket_api, monkeypatch, endpoint
):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.sessions import list_conversations

    cleanup_started, cleanup_release, cleanup_finished = (threading.Event() for _ in range(3))
    waiting, acquired = threading.Event(), threading.Event()
    genuine_completion = litellm.acompletion

    async def observe_completion(*args, **kwargs):
        try:
            return await genuine_completion(*args, **kwargs)
        finally:
            # The actual SDK already sent HTTP. Keep its cleanup exit pending
            # so lease ordering can be observed without inventing model output.
            cleanup_started.set()
            settled = asyncio.create_task(asyncio.to_thread(cleanup_release.wait, 5))
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    continue
            assert settled.result(), "The test did not release the SDK cleanup gate"
            cleanup_finished.set()

    monkeypatch.setattr(litellm, "acompletion", observe_completion)

    def other_writer():
        with exclusive_lifecycle(kb_dir, on_wait=waiting.set):
            acquired.set()

    with ErrorTransport(status=503, gate=threading.Event()) as model:
        path = kb_dir / ".ultrakb/config.yaml"
        config = load_config(path)
        config.update(model="deepseek/deepseek-flash", language="en", timeout=10)
        config["model_policy"] = {"max_calls": 8, "retries": 2, "backoff": 0}
        config["role_generation"] = {"answer": {"max_tokens": 32, "reasoning_effort": "none"}}
        save_config(path, config)
        atomic_write_text(
            kb_dir / ".env", f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={model.url}\n"
        )
        publish_pages(kb_dir, {"sources/original.md": b"The answer in this original is 42."})
        question = "Read sources/original.md and answer the question."
        payload = {"kb": "local", "stream": True}
        payload.update(question=question, save=True) if endpoint == "query" else payload.update(
            message=question
        )
        with httpx.Client(trust_env=False, timeout=5) as client, ThreadPoolExecutor(1) as workers:
            writer = None
            try:
                with client.stream(
                    "POST",
                    f"{socket_api.url}/api/v1/{endpoint}",
                    json=payload,
                    headers={"Authorization": "Bearer local-lifecycle-token"},
                ) as response:
                    _lines = _start(response)
                    assert model.wait_for_requests(1, timeout=5), socket_api.frames
                    assert model.requests[0]["path"].endswith("/chat/completions")
                    assert model.requests[0]["json"]["stream"] is True
                    assert model.requests[0]["json"]["model"] == "deepseek-flash"
                # Closing httpx's streaming response closes an actual TCP socket.
                assert socket_api.disconnected.wait(3), "uvicorn did not observe socket disconnect"
                model.gate.set()
                assert cleanup_started.wait(3), "Disconnect never reached SDK cleanup"
                writer = workers.submit(other_writer)
                assert waiting.wait(3), "The other writer did not encounter the operation lease"
                assert not acquired.is_set() and not socket_api.finished.is_set()
                cleanup_release.set()
                writer.result(timeout=5)
                assert cleanup_finished.is_set() and socket_api.finished.wait(3)
            finally:
                cleanup_release.set()
                model.gate.set()
                if writer is not None:
                    writer.result(timeout=5)
        assert len(model.requests) == 1, "A disconnected application must not retry the model"
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))
    sessions = list_conversations(kb_dir)
    if endpoint == "chat":
        assert len(sessions) == 1
        restored = read_conversation(kb_dir, sessions[0]["id"])
        assert restored.total_turns == 0
        assert restored.turns == ()
        assert restored.incomplete == ((0, question),)
    else:
        assert sessions == []
    [receipt] = read_requests(kb_dir)
    assert receipt.send_status == "sent" and receipt.state in {"failed", "cancelled"}
    assert receipt.business_state == "cancelled"
    assert receipt.usage_status == "unknown"


@pytest.mark.integration
def test_tcp_upload_disconnect_retracts_only_owned_staging_before_kb_admission(
    kb_dir, socket_api, monkeypatch
):
    from ultrakb.api_uploads import cleanup_uploads, reserve_upload
    from ultrakb.application.uploads import published_input

    staged, worker_waiting = [], threading.Event()

    def observe_reserve(upload):
        value = reserve_upload(upload)
        staged.append(value)
        return value

    @contextmanager
    def observe_admission(*args, **kwargs):
        worker_waiting.set()
        with published_input(*args, **kwargs) as owned:
            yield owned

    monkeypatch.setattr("ultrakb.api_helpers.reserve_upload", observe_reserve)
    monkeypatch.setattr("ultrakb.api_helpers.published_input", observe_admission)
    other_owner = reserve_upload(UploadFile(filename="other.md", file=io.BytesIO(b"other")))
    atomic_write_text(other_owner[0], "Other request's private input")
    existing = kb_dir / "raw/existing.md"
    atomic_write_text(existing, "Previously retained raw input")
    try:
        with (
            kb_ingest_lock(kb_dir / ".ultrakb"),
            httpx.Client(trust_env=False, timeout=5) as client,
        ):
            with client.stream(
                "POST",
                f"{socket_api.url}/api/v1/add",
                data={"kb": "local", "stream": "true"},
                files=[("files", ("paper.md", b"# complete private upload", "text/markdown"))],
                headers={"Authorization": "Bearer local-lifecycle-token"},
            ) as response:
                _lines = _start(response)
                assert worker_waiting.wait(3)
                assert len(staged) == 1
                assert staged[0][0].read_bytes() == b"# complete private upload"
                assert not (kb_dir / "raw/paper.md").exists()
            assert socket_api.disconnected.wait(3)
            assert socket_api.finished.wait(3), "Disconnected upload still waits for the KB lease"
            assert not staged[0][0].exists() and not staged[0][0].parent.exists()
            assert other_owner[0].read_text() == "Other request's private input"
            assert existing.read_text() == "Previously retained raw input"
            assert not (kb_dir / "raw/paper.md").exists()
        assert read_requests(kb_dir) == []
    finally:
        cleanup_uploads([other_owner])


@pytest.mark.live
def test_tcp_disconnect_after_real_flash_delta_drains_sdk_without_committing_half_turn(
    kb_dir, socket_api, live_llm, monkeypatch
):
    from litellm import CustomStreamWrapper

    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.sessions import list_conversations

    live_llm.install(monkeypatch)
    config_path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(config_path)
    config.update(model="deepseek/deepseek-flash", language="en", timeout=30)
    config["model_policy"] = {"max_calls": 2, "retries": 0}
    config["role_generation"] = {"answer": {"max_tokens": 96, "reasoning_effort": "none"}}
    save_config(config_path, config)
    publish_pages(
        kb_dir, {"sources/original.md": "The twelve months run January–December.".encode()}
    )
    sdk_waiting, release_sdk, sdk_closed = (threading.Event() for _ in range(3))
    writer_waiting, writer_acquired = threading.Event(), threading.Event()
    original_next, original_close = CustomStreamWrapper.__anext__, CustomStreamWrapper.aclose
    delivered, text_stream = False, None
    decoded_usage = {}
    sdk_calls_before, requests_before = len(live_llm.calls), len(live_llm.requests)
    receipts_before, reused_before = len(live_llm.receipts), live_llm.reused

    async def gated_chunk(stream):
        nonlocal delivered, text_stream
        if delivered and not release_sdk.is_set():
            sdk_waiting.set()
            settled = asyncio.create_task(asyncio.to_thread(release_sdk.wait, 30))
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    continue
            assert settled.result(), "The test did not release the SDK stream gate"
        chunk = await original_next(stream)
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            # Observe exactly what the business SDK receives, independently of
            # the gateway's already-drained provider billing receipt.
            decoded_usage[id(stream)] = usage.model_dump()
        if any((choice.delta.content or "").strip() for choice in chunk.choices):
            delivered = True
            text_stream = stream
        return chunk

    async def observe_close(stream):
        result = await original_close(stream)
        if stream is text_stream:
            sdk_closed.set()
        return result

    monkeypatch.setattr(CustomStreamWrapper, "__anext__", gated_chunk)
    monkeypatch.setattr(CustomStreamWrapper, "aclose", observe_close)

    def other_writer():
        with exclusive_lifecycle(kb_dir, on_wait=writer_waiting.set):
            assert sdk_closed.is_set(), "The write lease escaped before the actual SDK closed"
            writer_acquired.set()

    question = (
        "Format demonstration using only this question: list the twelve English month names "
        "in order. Do not research or use tools."
    )
    with httpx.Client(trust_env=False, timeout=45) as client, ThreadPoolExecutor(1) as workers:
        writer = None
        try:
            with client.stream(
                "POST",
                f"{socket_api.url}/api/v1/chat",
                json={"kb": "local", "stream": True, "message": question},
                headers={"Authorization": "Bearer local-lifecycle-token"},
            ) as response:
                lines = _start(response)
                event = None
                for line in lines:
                    if line.startswith("event: "):
                        event = line.removeprefix("event: ")
                        assert event not in {"error", "final", "done"}, socket_api.frames
                    elif event == "delta" and line.startswith("data: "):
                        text = json.loads(line.removeprefix("data: "))["text"]
                        if text.strip():
                            break
                else:
                    pytest.fail("The actual Flash text delta never reached the TCP client")
                assert delivered and sdk_waiting.wait(3)
            assert socket_api.disconnected.wait(3)
            writer = workers.submit(other_writer)
            assert writer_waiting.wait(3)
            assert not writer_acquired.is_set() and not socket_api.finished.is_set()
            release_sdk.set()
            writer.result(timeout=30)
            assert sdk_closed.is_set() and socket_api.finished.wait(3)
        finally:
            release_sdk.set()
            if writer is not None:
                writer.result(timeout=30)
    calls = live_llm.calls[sdk_calls_before:]
    requests = live_llm.requests[requests_before:]
    assert 1 <= len(calls) == len(requests) <= 2
    assert all(item["model"] == "deepseek/deepseek-flash" and item["stream"] for item in calls)
    assert not any("event: final\n" in frame for frame in socket_api.frames)
    [saved] = list_conversations(kb_dir)
    restored = read_conversation(kb_dir, saved["id"])
    assert restored.total_turns == 0
    assert restored.turns == ()
    assert restored.incomplete == ((0, question),)
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))
    [receipt] = [item for item in read_requests(kb_dir) if item.business_state == "cancelled"]
    assert receipt.state == "cancelled" and receipt.send_status == "sent"
    if usage := decoded_usage.get(id(text_stream)):
        assert receipt.usage_status == "known"
        assert receipt.input_total == usage["prompt_tokens"] > 0
        assert receipt.output_total == usage["completion_tokens"] > 0
    else:
        # Cancellation may close the business SDK before the final usage frame.
        # Independent gateway accounting must never become invented KB usage.
        assert receipt.usage_status == "unknown"
        assert receipt.input_total is None and receipt.output_total is None
        assert receipt.cached_input is None and receipt.reasoning_output is None
    if len(live_llm.receipts) > receipts_before:
        assert live_llm.receipts[-1]["usage"]["completion_tokens"] > 0
    else:
        assert live_llm.reused > reused_before  # Unchanged genuine receipt from this same run.
