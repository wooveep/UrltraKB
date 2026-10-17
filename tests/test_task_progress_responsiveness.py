"""A real worker must receive safe stop while parent usage projection is stalled."""

import threading
import time
from pathlib import Path

import pytest
from error_transport import ErrorTransport


@pytest.mark.integration
def test_usage_backlog_keeps_task_reads_and_safe_stop_responsive(kb_dir, tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.lifecycle import current_generation
    from ultrakb.locks import atomic_write_text
    from ultrakb.runtime.records import UnitIdentity, read_receipt
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    settings = tmp_path / "settings"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", settings)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", settings / "global.yaml")
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: openai/gpt-4o-mini\nlanguage: en\ntimeout: 10\n"
        "model_capacity:\n  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  max_calls: 2\n  concurrency: 1\n  retries: 0\n",
    )
    source = kb_dir.parent / f"{kb_dir.name}-source.md"
    atomic_write_text(source, "# Maintenance\n\nClose the inlet before maintenance.")
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    execution_records = kb_dir / ".ultrakb/usage/executions"
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        # Only the parent's real progress merge is delayed. Spawned worker I/O
        # and its control reader remain live, and this read holds no ledger lock.
        if path.parent == execution_records and threading.current_thread().name == "ultrakb-tasks":
            entered.set()
            assert release.wait(15), "Test did not release the parent usage projection"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    gate = threading.Event()
    observer = manager = None
    failures = []
    with ErrorTransport(status=400, gate=gate) as service:
        atomic_write_text(
            kb_dir / ".env",
            f"LLM_API_KEY=offline-progress-key\nOPENAI_API_BASE={service.url}\n",
        )
        history = tmp_path / "history"
        manager = TaskManager(history_dir=history, max_workers=1)
        try:
            request = ImportFile(str(source))
            task_id = manager.submit(kb_dir, [request])
            identity = UnitIdentity.create(
                task_id, 0, str(kb_dir), request, generation=current_generation(kb_dir)
            )
            assert entered.wait(10), "Parent never merged the worker's real usage progress"
            assert service.wait_for_requests(1), "Worker never reached the gated HTTP request"
            assert not read_receipt(history / "units", identity)

            def observe_and_stop():
                try:
                    view = manager.get(task_id)
                    assert view.started_at is not None and not view.processes_reaped
                    assert any(task.id == task_id for task in manager.tasks())
                    manager.stop(task_id)
                    assert manager.get(task_id).stop_requested
                except BaseException as error:
                    failures.append(error)
                finally:
                    observed.set()

            observer = threading.Thread(target=observe_and_stop)
            observer.start()
            assert observed.wait(0.25), "Status reads and safe stop waited for projection I/O"
            assert not failures
            gate.set()
            deadline = time.monotonic() + 10
            receipt = None
            while receipt is None and time.monotonic() < deadline:
                receipt = read_receipt(history / "units", identity)
                if receipt is None:
                    time.sleep(0.01)
            assert receipt is not None and receipt.status == "stopped", receipt
            assert not release.is_set(), "Worker stop waited for the parent's usage merge"
            assert len(service.requests) == 1
            release.set()
            result = manager.wait(task_id, timeout=15)
            assert result.state == "stopped" and result.stop_requested and result.processes_reaped
            assert result.results[0].status == "stopped"
            assert result.model_usage["current"]["actual_sends"] == 1
        finally:
            gate.set()
            release.set()
            if observer is not None:
                observer.join(5)
            manager.shutdown(stop=True)
            assert manager.join(15)
