"""Spawn isolation through the real SDK, with a controlled HTTP model boundary."""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from openkb.locks import atomic_write_text
from openkb.runtime.requests import AskQuestion
from openkb.runtime.tasks import TaskManager


@pytest.fixture
def model_service():
    requests = queue.Queue()
    arrived, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.put(
                (body["model"], self.headers.get("Authorization"), self.headers.get("X-KB"))
            )
            if body["model"] == "unit-a" and not arrived.is_set():
                arrived.set()
                release.wait(30)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": body["model"]}, None),
                ({}, "stop"),
            ]:
                event = {
                    "id": "completion-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, arrived, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def configure(kb, url, name):
    atomic_write_text(
        kb / ".openkb/config.yaml",
        yaml.safe_dump(
            {
                "model": f"openai/{name}",
                "language": "en",
                "pageindex_threshold": 20,
                "extra_headers": {"X-KB": name},
                "timeout": 5,
            }
        ),
    )
    atomic_write_text(kb / ".env", f"LLM_API_KEY=synthetic-{name}\nOPENAI_API_BASE={url}\n")


def test_batch_keeps_first_snapshot_and_independent_kb_uses_its_own_settings(
    kb_dir,
    tmp_path,
    model_service,
):
    import shutil

    url, requests, arrived, release = model_service
    other = tmp_path.parent / (tmp_path.name + "-other")
    shutil.copytree(kb_dir, other)
    configure(kb_dir, url, "unit-a")
    configure(other, url, "unit-b")
    environment, cwd = dict(os.environ), os.getcwd()
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=2)
    try:
        batch = manager.submit(kb_dir, [AskQuestion("First"), AskQuestion("Second")])
        assert arrived.wait(30), manager.get(batch)
        independent = manager.submit(other, [AskQuestion("Independent")])
        assert manager.wait(independent, timeout=30).results[0].output == "unit-b"
        # An external editor changes settings after actual first execution.
        # Later batch units keep the acknowledged context; a new task gets it.
        configure(kb_dir, url, "unit-new")
        queued = manager.submit(kb_dir, [AskQuestion("Queued")])
        release.set()
        finished = manager.wait(batch, timeout=45)
        assert finished.state == "completed", finished
        assert [result.output for result in finished.results] == ["unit-a", "unit-a"]
        assert manager.wait(queued, timeout=45).results[0].output == "unit-new"
        seen = [requests.get_nowait() for _ in range(requests.qsize())]
        assert sorted(seen) == sorted(
            [
                ("unit-a", "Bearer synthetic-unit-a", "unit-a"),
                ("unit-a", "Bearer synthetic-unit-a", "unit-a"),
                ("unit-b", "Bearer synthetic-unit-b", "unit-b"),
                ("unit-new", "Bearer synthetic-unit-new", "unit-new"),
            ]
        )
        assert os.getcwd() == cwd
        assert dict(os.environ) == environment
        summaries = "".join(path.read_text() for path in (tmp_path / "history").rglob("*.json"))
        assert "synthetic-" not in summaries
        assert "First" not in summaries
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(45)


def test_worker_loss_is_interrupted_and_never_replays_remaining_units(
    kb_dir,
    tmp_path,
    model_service,
):
    import multiprocessing

    url, requests, arrived, release = model_service
    configure(kb_dir, url, "unit-a")
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        task = manager.submit(kb_dir, [AskQuestion("Crash"), AskQuestion("Must not run")])
        assert arrived.wait(30)
        # Fault injection at the actual OS process boundary, not a fabricated
        # terminal message or a mocked business function.
        children = [
            process
            for process in multiprocessing.active_children()
            if process.name.startswith(f"openkb-unit-{task[:8]}-")
        ]
        assert len(children) == 1
        children[0].terminate()
        result = manager.wait(task, timeout=30)
        assert result.state == "interrupted"
        assert result.succeeded == 0
        assert result.unfinished == 2
        assert result.processes_reaped
        assert requests.qsize() == 1
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(30)
