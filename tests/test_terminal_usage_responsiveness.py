"""Terminal ledger reconciliation must not own the task manager's UI lock."""

import threading

import pytest

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.pages import read_page
from ultrakb.runtime.requests import SavePage
from ultrakb.runtime.tasks import TaskManager


@pytest.mark.integration
def test_task_reads_and_stop_do_not_wait_for_terminal_ledger_audit(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    initialize_kb(kb, seed_environment=False)
    from ultrakb.application import task_usage
    from ultrakb.desktop.verification_data import publish_pages

    publish_pages(kb, {"concepts/maintenance.md": b"# Maintenance\n\nOriginal."})
    opened = read_page(kb, "concepts/maintenance")
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    original = task_usage.task_usage_receipt

    def read(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Test did not release the native terminal query"
        return original(*args, **kwargs)

    monkeypatch.setattr(task_usage, "task_usage_receipt", read)
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
