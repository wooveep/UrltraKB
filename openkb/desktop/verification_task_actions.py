"""Native retry consent and history cleanup against actual spawn workers."""


def verify_task_actions(window, kb, wait_until):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.application.pages import read_page
    from openkb.desktop.task_actions import RetryDialog, clear_selected_history
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.runtime.records import TERMINAL
    from openkb.runtime.requests import SavePage

    first, second = kb / "wiki/concepts/retry-first.md", kb / "wiki/concepts/retry-second.md"
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(first, "First original")
        atomic_write_text(second, "Second original")
    versions = [read_page(kb, f"concepts/retry-{name}").version for name in ("first", "second")]
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(second, "Concurrent edit")
    task_id = window.manager.submit(
        kb,
        [
            SavePage("concepts/retry-first", "First saved", versions[0]),
            SavePage("concepts/retry-second", "Second saved", versions[1]),
        ],
    )
    wait_until(lambda: window.manager.get(task_id).state in TERMINAL)
    assert window.manager.get(task_id).state == "partial"
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(first, "Keep later work")
        atomic_write_text(second, "Second original")
    dialog = RetryDialog(window, task_id)
    dialog.show()
    try:
        wait_until(dialog.retry_button.isEnabled)
        assert dialog.preview.indices == (1,)
        dialog.grab().save(str(kb.parent / "native-task-retry.png"))
        dialog.retry_button.click()
        wait_until(lambda: any(task.retry_of == task_id for task in window.manager.tasks()))
        retried = next(task.id for task in window.manager.tasks() if task.retry_of == task_id)
        wait_until(lambda: window.manager.get(retried).state in TERMINAL)
        assert window.manager.get(retried).succeeded == 1
        assert first.read_text() == "Keep later work" and second.read_text() == "Second saved"
    finally:
        dialog.reject()
    confirmer = QTimer()

    def confirm():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox) and modal.windowTitle() == "清理任务摘要":
            modal.button(QMessageBox.StandardButton.Yes).click()

    confirmer.timeout.connect(confirm)
    confirmer.start(100)
    try:
        clear_selected_history(window, [task_id, retried])
        wait_until(
            lambda: all(task.id not in {task_id, retried} for task in window.manager.tasks())
        )
        assert first.read_text() == "Keep later work" and second.read_text() == "Second saved"
    finally:
        confirmer.stop()
