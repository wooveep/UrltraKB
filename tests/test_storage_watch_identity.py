"""Watch imports keep captured storage identity and release setup locks before models."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from stream_transport import StreamTransport

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.config import save_config
from ultrakb.locks import kb_ingest_lock
from ultrakb.watch_service import WatcherState, _process_file, _run_worker


def test_watch_worker_refuses_a_replaced_database_before_queued_work(tmp_path, monkeypatch):
    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root, seed_environment=False)
    initialize_kb(replacement, seed_environment=False)
    source = root / "raw/new.md"
    source.write_text("A file queued for the original store.")
    state = WatcherState("watched", root, root / "raw", 0, 0)
    calls = []
    monkeypatch.setattr("ultrakb.watch_service._add_for_api", lambda *a, **kw: calls.append(a))
    state.queue.put([str(source)])
    state.queue.put(None)
    os.replace(root / ".ultrakb", tmp_path / "old-state")
    os.replace(replacement / ".ultrakb", root / ".ultrakb")
    _run_worker(state)
    assert calls == [] and not state.running.is_set()
    assert any(event["event"] == "watcher_stopped" for event in state.events)


def test_http_watch_model_wait_does_not_hold_the_ingest_setup_lock(tmp_path):
    root = tmp_path / "kb"
    initialize_kb(root, seed_environment=False)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "model_policy": {"max_calls": 2, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    source = root / "raw/value.md"
    source.write_text("The stored value is 17.")
    state = WatcherState("watched", root, root / "raw", 0, 0)
    release = Event()

    def busy():
        raise AssertionError("A model wait still owns the ingest setup lock")

    responses = [
        {"content": json.dumps({"description": "Value", "content": "The stored value is 17."})},
        {"content": '{"concepts":{},"entities":{}}'},
    ]
    with (
        StreamTransport(responses, gate=release) as service,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        (root / ".env").write_text(f"LLM_API_KEY=offline-watch\nOPENAI_API_BASE={service.url}\n")
        future = pool.submit(_process_file, state, str(source))
        try:
            assert service.wait_for_requests(1)
            with kb_ingest_lock(root / ".ultrakb", on_wait=busy):
                pass
        finally:
            release.set()
            future.result(timeout=15)
        assert len(service.requests) == 2
        assert any(event["event"] == "file_done" for event in state.events)
