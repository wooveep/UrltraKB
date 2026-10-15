"""Spawn isolation through the real SDK, with a controlled HTTP model boundary."""

import os
import queue
import threading

import pytest
import yaml

from ultrakb.locks import atomic_write_text
from ultrakb.runtime.requests import AskQuestion
from ultrakb.runtime.tasks import TaskManager


@pytest.fixture
def model_service(live_llm, monkeypatch):
    """Gate genuine gateway delivery without replacing provider frames or usage."""
    import hmac

    live_llm.install(monkeypatch)
    requests = queue.Queue()
    record_start = len(live_llm._proxy.records)
    requests.delivered_records = lambda: live_llm._proxy.records[record_start:]
    arrived, release = threading.Event(), threading.Event()
    handler = live_llm._proxy._server.RequestHandlerClass
    forward = handler.do_POST
    credential = live_llm.credentials.api_key

    def observe(request):
        profile = request.headers.get("X-KB")
        captured = hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + credential
        )
        requests.put((profile, captured))
        if profile == "unit-a" and not arrived.is_set():
            arrived.set()
            release.wait(90)
            if getattr(release, "discard", False):
                return
        forward(request)

    monkeypatch.setattr(handler, "do_POST", observe)
    try:
        yield live_llm._proxy.url, requests, arrived, release
    finally:
        release.set()


def configure(kb, url, name, *, max_calls=1):
    atomic_write_text(kb / "wiki/sources/model-fixture.md", "An available source.")
    atomic_write_text(
        kb / ".ultrakb/config.yaml",
        yaml.safe_dump(
            {
                "model": "deepseek/deepseek-flash",
                "language": "en",
                "pageindex_threshold": 20,
                "extra_headers": {"X-KB": name},
                "model_policy": {"max_calls": max_calls, "retries": 0},
                "role_generation": {"answer": {"max_tokens": 64, "reasoning_effort": "none"}},
                "timeout": 90,
                "model_capacity": {
                    "max_input_tokens": 65536,
                    "tokenizer_model": "gpt-4o-mini",
                },
            }
        ),
    )
    atomic_write_text(kb / ".env", f"OPENAI_API_BASE={url}\n")


def test_batch_keeps_first_snapshot_and_independent_kb_uses_its_own_settings(
    kb_dir,
    tmp_path,
    model_service,
):
    import shutil

    url, requests, arrived, release = model_service
    other = tmp_path.parent / (tmp_path.name + "-other")
    shutil.copytree(kb_dir, other)
    # max_calls belongs to the entire task, including later batch workers.
    configure(kb_dir, url, "unit-a", max_calls=4)
    configure(other, url, "unit-b", max_calls=2)
    for root in (kb_dir, other):
        atomic_write_text(root / "wiki/index.md", "# Index\nProtocol acknowledgement fixture.")

    def question(label):
        return AskQuestion(
            f"{label}: Use read_file for index.md exactly once, then reply with the "
            "single word ACK. No other lookup or source facts are required."
        )

    environment, cwd = dict(os.environ), os.getcwd()
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=2)
    try:
        batch = manager.submit(
            kb_dir,
            [
                question("First"),
                question("Second"),
            ],
        )
        assert arrived.wait(30), manager.get(batch)
        independent = manager.submit(other, [question("Independent")])
        independent_result = manager.wait(independent, timeout=120)
        assert independent_result.state == "completed", independent_result
        assert independent_result.text.strip() == "ACK"
        # An external editor changes settings after actual first execution.
        # Later batch units keep the acknowledged context; a new task gets it.
        configure(kb_dir, url, "unit-new", max_calls=2)
        queued = manager.submit(kb_dir, [question("Queued")])
        release.set()
        finished = manager.wait(batch, timeout=120)
        assert finished.state == "completed", finished
        assert finished.text.strip() == "ACK"
        queued_result = manager.wait(queued, timeout=120)
        assert queued_result.state == "completed", queued_result
        assert queued_result.text.strip() == "ACK"
        task_counts = {}
        for task in (independent_result, finished, queued_result):
            assert len(task.results) == task.total
            assert task.succeeded == task.total
            assert task.error is None
            cumulative = 0
            model_task_ids = set()
            for result in task.results:
                assert result.status == "completed"
                assert result.output_state == "available"
                assert result.output.strip() == "ACK"
                if result.answer_outcome == "partial":
                    assert result.unfinished == ("Unverified required parts after call_budget",)
                else:
                    assert result.answer_outcome == "answered"
                    assert result.unfinished == ()
                assert result.usage["output"] > 0
                current = result.model_usage["current"]
                assert 1 <= current["actual_sends"] == current["requests"] <= 2
                assert current["unsent_attempts"] == 0
                cumulative += current["actual_sends"]
                [model_task] = result.model_usage["model_tasks"]
                model_task_ids.add(model_task["task_id"])
                assert model_task["actual_sends"] == cumulative
                assert model_task["limit"] == (4 if task.id == batch else 2)
            assert len(model_task_ids) == 1
            task_counts[task.id] = cumulative
        seen = [requests.get_nowait() for _ in range(requests.qsize())]
        assert all(captured for _, captured in seen)
        from collections import Counter

        assert Counter(profile for profile, _ in seen) == {
            "unit-a": task_counts[batch],
            "unit-b": task_counts[independent],
            "unit-new": task_counts[queued],
        }
        records = requests.delivered_records()
        assert len(records) == sum(task_counts.values()) == len(seen)
        assert 4 <= len(records) <= 8
        assert all(payload["model"] == "deepseek-flash" for payload, _, _ in records)
        assert all(payload["max_tokens"] == 64 for payload, _, _ in records)
        assert os.getcwd() == cwd
        environment_is_unchanged = dict(os.environ) == environment
        assert environment_is_unchanged
        summaries = "".join(path.read_text() for path in (tmp_path / "history").rglob("*.json"))
        assert "Bearer " not in summaries
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
        task = manager.submit(
            kb_dir,
            [AskQuestion("Crash: reply with the single word ACK."), AskQuestion("Must not run")],
        )
        assert arrived.wait(30)
        # Fault injection at the actual OS process boundary, not a fabricated
        # terminal message or a mocked business function.
        children = [
            process
            for process in multiprocessing.active_children()
            if process.name.startswith(f"ultrakb-unit-{task[:8]}-")
        ]
        assert len(children) == 1
        children[0].terminate()
        release.discard = True
        result = manager.wait(task, timeout=120)
        assert result.state == "interrupted"
        assert result.succeeded == 0
        assert result.unfinished == 2
        assert result.processes_reaped
        assert requests.qsize() == 1
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_streamed_usage_reaches_receipt_and_restored_conversation(kb_dir, tmp_path, model_service):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.runtime.requests import ContinueConversation

    url, _, _, release = model_service
    configure(kb_dir, url, "unit-b")
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        first = manager.wait(
            manager.submit(kb_dir, [ContinueConversation("Reply with the single word ACK.")]),
            timeout=120,
        )
        assert first.state == "completed", first
        result = first.results[0]
        assert result.usage["input_cached"] >= 0
        assert result.usage["input_uncached"] > 0 and result.usage["output"] > 0
        second = manager.wait(
            manager.submit(
                kb_dir, [ContinueConversation("Repeat that same word.", result.session_id)]
            ),
            timeout=120,
        )
        assert second.state == "completed", second
        restored = read_conversation(kb_dir, result.session_id)
        from ultrakb.agent.token_usage import add_usage

        assert restored.usage == add_usage(result.usage, second.results[0].usage)
        assert restored.usage["output"] > result.usage["output"]
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(30)
