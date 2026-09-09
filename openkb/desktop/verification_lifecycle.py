"""Real Qt/worker shutdown and restart checks; OS tray clicks remain a separate check."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
)

from openkb.application.knowledge_bases import initialize_kb
from openkb.application.pages import read_page
from openkb.desktop.window import Workbench
from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.runtime.requests import SavePage


def _hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".lock"
    }


def verify_lifecycle(app, root: Path, mode: str, previous: Path | None) -> int:
    def wait(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            assert time.monotonic() < deadline, "Lifecycle check timed out"
            time.sleep(0.01)
        app.processEvents()

    evidence = {"mode": mode, "pid": os.getpid(), "executable": sys.executable}
    wait_for_tasks = mode in {"wait", "delete"}
    state = previous.resolve(strict=True) if previous else root
    if mode == "restart":
        assert previous is not None, "Restart requires --lifecycle-state"
        saved = json.loads((state / "lifecycle.json").read_text("utf-8"))
        assert saved["mode"] in {"wait", "stop", "delete"}
    else:
        assert previous is None, "Only restart accepts --lifecycle-state"
        initialize_kb(state / "kb", seed_environment=False, model="openai/lifecycle-verification")
        for number in range(3):
            atomic_write_text(state / f"kb/wiki/concepts/page-{number}.md", "Original\n")
        if mode == "delete":
            initialize_kb(state / "delete-kb", seed_environment=False)
    window = Workbench(history_dir=state / "history")
    window.show()
    try:
        kb = state / "kb"
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and window.page is not None)
        if mode == "restart":
            assert _hashes(kb) == saved["kb_hashes"], "Restart changed stored KB files"
            before = [task.summary() for task in window.manager.tasks()]
            assert len(before) == 1 and before[0]["id"] == saved["task_id"]
            assert not window.watch_registry.watches()
            started = time.monotonic()
            wait(lambda: time.monotonic() - started >= 2)
            assert [task.summary() for task in window.manager.tasks()] == before
            assert not window.manager.has_work(kb)
            assert _hashes(kb) == saved["kb_hashes"]
            evidence["history_retained_without_replay_or_watch"] = True
            window.request_quit()
        else:
            watch = window.watch_registry.start(kb)
            wait(lambda: watch.view().scans > 0)
            available = QSystemTrayIcon.isSystemTrayAvailable() and window.tray.isVisible()
            window.close()
            app.processEvents()
            assert window.isVisible() != available
            evidence["tray_available"] = available
            evidence["close_result"] = "hidden-to-tray" if available else "visible-fallback"
            # Trigger the real menu action; this does not claim a physical OS tray click.
            window.tray.contextMenu().actions()[0].trigger()
            assert window.isVisible()
            requests = [
                SavePage(
                    f"concepts/page-{number}",
                    f"Completed {number}\n",
                    read_page(kb, f"concepts/page-{number}").version,
                )
                for number in range(3)
            ]
            with ExitStack() as scope:
                scope.enter_context(kb_ingest_lock(kb / ".openkb"))
                task_id = window.manager.submit(kb, requests)
                wait(lambda: window.manager.get(task_id).state == "waiting")
                window.hide()
                choice = "等待完成并退出" if wait_for_tasks else "安全停止并退出"

                def choose():
                    box = QApplication.activeModalWidget()
                    if isinstance(box, QMessageBox):
                        next(button for button in box.buttons() if button.text() == choice).click()
                    else:
                        QTimer.singleShot(10, choose)

                QTimer.singleShot(10, choose)
                quit_action = window.tray.contextMenu().actions()[1]
                if mode == "delete":
                    from openkb.desktop.knowledge_bases import KnowledgeBasesDialog
                    from openkb.lifecycle import deletion_binding

                    target = state / "delete-kb"
                    scope.enter_context(kb_ingest_lock(target / ".openkb"))
                    dialog = KnowledgeBasesDialog(window)
                    dialog._remove(target, deletion_binding(target))
                    QTimer.singleShot(10, quit_action.trigger)
                    dialog.exec()
                    assert dialog._cancel and dialog._closed and target.exists()
                    assert QApplication.activeModalWidget() is None
                    evidence["pending_deletion_modal_revoked"] = True
                elif mode == "wait":
                    QTimer.singleShot(10, quit_action.trigger)
                    window._import_urls()
                    assert QApplication.activeModalWidget() is None
                    evidence["business_modal_revoked"] = True
                else:
                    quit_action.trigger()
                assert window._quitting and window.isVisible() and window.isEnabled()
                assert all(not control.isEnabled() for control in window.shutdown_controls)
                wait(window.watch_registry.stopped)
                atomic_write_text(kb / "raw/after-shutdown.md", "Must not start another task\n")
                window._poll_tasks()
                window.task_table.selectRow(0)
                details = next(
                    button
                    for button in window.findChildren(QPushButton)
                    if button.text() == "查看所选任务结果"
                )
                assert details.isEnabled() and window.task_table.isEnabled()
                observed = []

                def inspect_details():
                    dialog = QApplication.activeModalWidget()
                    text = dialog.findChild(QPlainTextEdit, "task-status").toPlainText()
                    observed.append(task_id in text and "阶段：" in text)
                    dialog.accept()

                QTimer.singleShot(10, inspect_details)
                details.click()
                assert observed == [True]
                if wait_for_tasks:
                    window.close()
                    assert window.isVisible(), "Pending shutdown lost its observation window"
                    assert window.manager.get(task_id).succeeded == 0
                window.grab().save(str(root / "shutdown.png"))
                evidence["shutdown_progress_and_diagnostics_visible"] = True
            wait(lambda: window.manager.join(0), timeout=60)
            task = window.manager.get(task_id)
            assert task.processes_reaped
            assert len(window.manager.tasks()) == 1, "Watching admitted work after shutdown"
            assert task.succeeded == (3 if wait_for_tasks else 0), task
            assert task.state == ("completed" if wait_for_tasks else "stopped"), task
            for number in range(3):
                page = read_page(kb, f"concepts/page-{number}")
                assert page.body.strip() == (
                    f"Completed {number}" if wait_for_tasks else "Original"
                )
            evidence.update(task_id=task_id, task=task.summary(), kb_hashes=_hashes(kb))
        wait(window.shutdown_complete)
        evidence["owned_work_reaped"] = True
        (root / "lifecycle.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"output": str(root), "mode": mode, "passed": True}))
        return 0
    finally:
        window.watch_registry.stop_all()
        window.manager.shutdown(stop=True)
        window.io.stop()
        window.reader.stop_rendering()
        window.chat.stop_rendering()
        wait(
            lambda: window.manager.join(0)
            and window.watch_registry.stopped()
            and window.io.stopped()
            and window.reader.rendering_stopped()
            and window.chat.rendering_stopped()
        )
        window.timer.stop()
        window.tray.hide()
        window.close()
