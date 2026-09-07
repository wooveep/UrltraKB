"""Manual retries verify committed units; history cleanup never removes artifacts."""

import pytest

from openkb.application.pages import read_page
from openkb.runtime.requests import SavePage
from openkb.runtime.task_actions import preview_retry, retry_task
from openkb.runtime.tasks import TaskManager


def test_interrupted_history_can_be_reviewed_and_cleared_without_claiming_reaped(kb_dir, tmp_path):
    import json
    import uuid

    from openkb.runtime.records import TaskView, UnitIdentity

    history = tmp_path / "history"
    history.mkdir()
    task_id = uuid.uuid4().hex
    request = SavePage("concepts/note", "new", "old-version")
    from dataclasses import asdict

    view = TaskView(task_id, str(kb_dir), "SavePage", "running", "saving", 1, (), False, False)
    (history / f"{task_id}.json").write_text(
        json.dumps(
            {
                "view": view.summary(),
                "identities": [asdict(UnitIdentity.create(task_id, 0, str(kb_dir), request))],
            }
        )
    )
    manager = TaskManager(history_dir=history)
    try:
        restored = manager.wait(task_id)
        assert restored.state == "interrupted" and not restored.processes_reaped
        preview = preview_retry(manager, task_id)
        assert not preview.allowed and "未能确认" in preview.reason
        manager.clear_history([task_id])
        assert not manager.tasks()
        assert not (history / f"{task_id}.json").exists()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_history_cleanup_does_not_hold_scheduler_lock(kb_dir, tmp_path, monkeypatch):
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    page = kb_dir / "wiki/concepts/note.md"
    page.write_text("old")
    manager = TaskManager(history_dir=tmp_path / "history")
    entered, release = Event(), Event()
    original = shutil.rmtree

    def slow_delete(path, *args, **kwargs):
        if path == manager.receipt_dir / task:
            entered.set()
            assert release.wait(10)
        return original(path, *args, **kwargs)

    try:
        task = manager.submit(
            kb_dir, [SavePage("concepts/note", "first", read_page(kb_dir, "concepts/note").version)]
        )
        assert manager.wait(task).succeeded == 1
        monkeypatch.setattr(shutil, "rmtree", slow_delete)
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(manager.clear_history, [task])
            try:
                assert entered.wait(5)
                assert pool.submit(manager.tasks).result(timeout=1)
                with pytest.raises(ValueError, match="清理"):
                    manager.retry_inputs(task)
                manager.stop(task)
                other = manager.submit(
                    kb_dir,
                    [
                        SavePage(
                            "concepts/note", "second", read_page(kb_dir, "concepts/note").version
                        )
                    ],
                )
                assert manager.wait(other, timeout=5).succeeded == 1
            finally:
                release.set()
                cleanup.result(timeout=5)
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_manual_retry_only_repeats_failed_units_as_a_new_task(kb_dir, tmp_path):
    first = kb_dir / "wiki/concepts/first.md"
    second = kb_dir / "wiki/concepts/second.md"
    first.write_text("first original")
    second.write_text("second original")
    first_version = read_page(kb_dir, "concepts/first").version
    second_version = read_page(kb_dir, "concepts/second").version
    second.write_text("external edit before execution")
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        task = manager.submit(
            kb_dir,
            [
                SavePage("concepts/first", "first completed", first_version),
                SavePage("concepts/second", "second completed", second_version),
            ],
        )
        result = manager.wait(task, timeout=30)
        assert result.state == "partial" and result.succeeded == 1 and result.failed == 1
        first.write_text("manual edit after successful unit")
        second.write_text("second original")
        preview = preview_retry(manager, task)
        assert preview.indices == (1,) and preview.allowed
        assert str(first) in preview.retained
        retried = retry_task(manager, preview)
        assert retried != task
        assert manager.wait(retried, timeout=30).succeeded == 1
        assert manager.get(retried).retry_of == task
        assert first.read_text() == "manual edit after successful unit"
        assert second.read_text() == "second completed"
        assert manager.get(task).state == "partial"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_retry_and_cleanup_refuse_active_task_and_respect_repair_gate(kb_dir, tmp_path):
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import RecoveryRequired, repair_marker

    page = kb_dir / "wiki/concepts/note.md"
    page.write_text("old")
    version = read_page(kb_dir, "concepts/note").version
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            task = manager.submit(kb_dir, [SavePage("concepts/note", "new", version)])
            with pytest.raises(ValueError):
                preview_retry(manager, task)
            with pytest.raises(ValueError):
                manager.clear_history([task])
            manager.stop(task)
        manager.wait(task, timeout=30)
        repair_marker(kb_dir).write_text('{"error_type": "RetainedEvidence"}')
        with pytest.raises(RecoveryRequired):
            preview_retry(manager, task)
        assert repair_marker(kb_dir).exists()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_history_cleanup_removes_only_terminal_summary_and_receipts(kb_dir, tmp_path):
    page = kb_dir / "wiki/concepts/note.md"
    page.write_text("old")
    version = read_page(kb_dir, "concepts/note").version
    history = tmp_path / "history"
    manager = TaskManager(history_dir=history)
    try:
        task = manager.submit(kb_dir, [SavePage("concepts/note", "retained artifact", version)])
        assert manager.wait(task, timeout=30).succeeded == 1
        manager.clear_history([task])
        assert not manager.tasks()
        assert not (history / f"{task}.json").exists()
        assert not (history / "units" / task).exists()
        assert page.read_text() == "retained artifact"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)
    restarted = TaskManager(history_dir=history)
    try:
        assert not restarted.tasks()
    finally:
        restarted.shutdown()
        assert restarted.join(30)
