"""Terminal ledger reconciliation must not own the task manager's UI lock."""

import threading
from pathlib import Path

import pytest

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.pages import read_page
from ultrakb.llm_usage import UsageRequest
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.runtime.requests import SavePage
from ultrakb.runtime.tasks import TaskManager


@pytest.mark.integration
def test_task_reads_and_stop_do_not_wait_for_terminal_ledger_audit(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    initialize_kb(kb, seed_environment=False)
    atomic_write_text(kb / "wiki/concepts/maintenance.md", "# Maintenance\n\nOriginal.")
    opened = read_page(kb, "concepts/maintenance")
    ledger = kb / ".ultrakb/usage/requests"
    identity = "d" * 32
    atomic_write_json(
        ledger / f"{identity}.json",
        UsageRequest(
            request_attempt_id=identity,
            execution_id="e" * 32,
            model="fixture",
            stage="summary",
            started_at="2026-10-18T00:00:00+00:00",
        ).model_dump(),
    )
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.parent == ledger:
            entered.set()
            assert release.wait(5), "Test did not release the terminal audit"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    observer = None
    try:
        task_id = manager.submit(kb, [SavePage("concepts/maintenance", "Updated.", opened.version)])
        assert entered.wait(10), "Terminal audit was not observed"

        def inspect():
            manager.get(task_id)
            manager.stop(task_id)
            observed.set()

        observer = threading.Thread(target=inspect)
        observer.start()
        assert observed.wait(0.25), "Status and stop waited for terminal ledger reconciliation"
        release.set()
        manager.wait(task_id, timeout=10)
    finally:
        release.set()
        if observer:
            observer.join(5)
        manager.shutdown(stop=True)
        assert manager.join(10)
