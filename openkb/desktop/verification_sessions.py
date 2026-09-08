"""Opt-in native conversation maintenance with real isolated workers."""

from pathlib import Path


def verify_sessions(window, kb, wait_until):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.agent.chat_session import ChatSession
    from openkb.desktop.sessions import SessionsDialog
    from openkb.desktop.verification_tasks import SubmittedTasks, assert_result_text
    from openkb.locks import session_lock
    from openkb.runtime.requests import ExportConversation

    session = ChatSession.new(kb, "openai/verification", "zh")
    session.record_turn("原生会话", "第一个完整回答。", [])
    from openkb.desktop.verification_workbench import management_page

    dialog = management_page(window, kb, "对话", SessionsDialog, wait_until)
    stale = True
    confirmer = QTimer()
    tasks = SubmittedTasks(window.manager, wait_until)

    def confirm():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox) and modal.windowTitle() == "确认删除对话":
            if stale:
                session.record_turn("确认之后的新问题", "必须保留的新回答。", [])
            modal.done(QMessageBox.StandardButton.Yes)

    def finish():
        task = tasks.finish(lambda: dialog._task is None)
        expected = (
            "会话已不存在，本项已跳过。"
            if task.skipped
            else "任务完成。"
            if task.state == "completed"
            else "任务未完成，请查看结果。"
        )
        assert dialog.status.text() == expected, dialog.status.text()
        assert_result_text(task, dialog.details.toPlainText(), resources=True)
        return task

    confirmer.timeout.connect(confirm)
    confirmer.start(100)
    try:
        wait_until(lambda: dialog.table.rowCount() == 1)
        dialog.table.selectRow(0)
        # A held conversation yields the child slot and can stop before capture.
        with session_lock(kb, session.id):
            dialog.export()
            task_id = dialog._task
            wait_until(lambda: window.manager.get(task_id).state == "waiting")
            assert window.manager.get(task_id).started_at is None
            window.manager.stop(task_id)
            stopped = finish()
            assert stopped.state == "stopped" and all(not r.resources for r in stopped.results)
        copies = []
        for _ in range(2):
            dialog.table.selectRow(0)
            dialog.export()
            exported = finish()
            assert exported.state == "completed" and exported.processes_reaped, exported
            copies.extend(exported.results[0].resources)
        assert len(set(copies)) == 2
        assert all("第一个完整回答" in Path(path).read_text(encoding="utf-8") for path in copies)
        dialog.table.selectRow(0)
        dialog.delete()
        conflict = finish()
        assert conflict.failed == 1 and conflict.started_at is None, conflict
        assert session.path.exists()
        stale = False
        dialog.table.selectRow(0)
        dialog.delete()
        deleted = finish()
        assert deleted.state == "completed" and deleted.processes_reaped, deleted
        assert not session.path.exists()
        assert all(Path(path).exists() for path in copies)
        dialog._task = window.manager.submit(kb, [ExportConversation(session.id)])
        missing = finish()
        assert missing.skipped == 1 and missing.started_at is None, missing
        assert missing.results[0].session_id == session.id
        assert "no longer exists" in missing.results[0].error
        assert "跳过" in dialog.status.text()
    finally:
        confirmer.stop()
        dialog.reject()
