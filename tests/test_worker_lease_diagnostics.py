"""Contention is a deferred task, not an execution failure in its diagnostic log."""

import time

import pytest

from openkb.inputs import prepared_input
from openkb.locks import kb_ingest_lock
from openkb.runtime.diagnostics import read_task_log
from openkb.runtime.requests import ContinueSource
from openkb.runtime.tasks import TaskManager
from openkb.sources import SourceStore


@pytest.mark.parametrize("stop", [True, False])
def test_waiting_continue_has_no_failure_traceback(kb_dir, tmp_path, model_service, stop):
    original = tmp_path / "note.md"
    original.write_text("The service listens on port 9473.")
    with prepared_input(original) as ready:
        source = SourceStore(kb_dir).intake(ready)
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            task_id = manager.submit(kb_dir, [ContinueSource(source.source_id, source.id)])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                view = manager.get(task_id)
                if view.state == "waiting" and view.processes_reaped:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Continue did not yield its worker while waiting for the KB")
            text = read_task_log(manager.history_dir / "logs" / task_id)
            assert not view.results and view.started_at is None
            assert len(model_service) == 0
            assert "Traceback" not in text, text
            assert "等待知识库" in text
            if stop:
                manager.stop(task_id)
                stopped = manager.wait(task_id, timeout=5)
                assert stopped.state == "stopped" and stopped.processes_reaped
        if not stop:
            completed = manager.wait(task_id, timeout=20)
            assert completed.state == "completed" and completed.processes_reaped
            assert len(completed.results) == 1
            assert completed.results[0].document.source_id == source.source_id
            assert len(model_service) > 0
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_execution_errors_still_keep_a_traceback(kb_dir, tmp_path):
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task_id = manager.submit(kb_dir, [ContinueSource("a" * 32, "b" * 64)])
        failed = manager.wait(task_id, timeout=10)
        assert failed.state == "failed" and failed.processes_reaped
        text = read_task_log(manager.history_dir / "logs" / task_id)
        assert "Traceback" in text
        assert "等待知识库" not in text
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def _worker_with_failed_preparation_cleanup(*args):
    from contextlib import contextmanager
    from unittest.mock import patch

    from openkb.runtime.input_store import child_preparation
    from openkb.runtime.worker import run_unit

    @contextmanager
    def failing_cleanup(path):
        with child_preparation(path):
            yield
        raise OSError("Input lease cleanup failed")

    with patch("openkb.runtime.input_store.child_preparation", failing_cleanup):
        run_unit(*args)


def test_deferred_worker_cleanup_failure_is_not_requeued(kb_dir, tmp_path, monkeypatch):
    monkeypatch.setattr("openkb.runtime.tasks.run_unit", _worker_with_failed_preparation_cleanup)
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            task_id = manager.submit(kb_dir, [ContinueSource("a" * 32, "b" * 64)])
            failed = manager.wait(task_id, timeout=3)
            assert failed.state == "failed" and failed.processes_reaped
            assert len(failed.results) == 1
            text = read_task_log(manager.history_dir / "logs" / task_id)
            assert "Input lease cleanup failed" in text
            assert "Traceback" in text
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
