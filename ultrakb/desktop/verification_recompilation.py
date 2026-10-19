"""Opt-in native selection/confirmation and real spawned recompilation."""

from __future__ import annotations


def verify_recompilation(window, kb, wait_until):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ultrakb.desktop.documents import DocumentsDialog
    from ultrakb.desktop.verification_data import publish_pages, retain_document
    from ultrakb.desktop.verification_tasks import SubmittedTasks, finished_document

    retain_document(kb, "短资料.md", "native-short", "# 原生重编译\n已有的来源正文。")
    retain_document(
        kb,
        "长资料.pdf",
        "native-long",
        "Retained long document.",
        pages=[{"page": 1, "content": "Retained long document."}],
        summary="---\nsources: [sources/native-long.json]\n---\n# 既有长文摘要\n",
    )
    # Managed originals remain usable after the external import paths disappear.
    (kb / "raw/短资料.md").unlink()
    (kb / "raw/长资料.pdf").unlink()
    from ultrakb.desktop.verification_workbench import management_page

    dialog = management_page(window, kb, "资料", DocumentsDialog, wait_until)
    confirmer = QTimer()
    stale_confirmation = True
    tasks = SubmittedTasks(window.manager, wait_until)

    def confirm():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox) and modal.windowTitle() == "确认重编译":
            if stale_confirmation:
                publish_pages(kb, {"concepts/after-confirmation.md": "手工编辑。".encode()})
            modal.done(QMessageBox.StandardButton.Yes)

    confirmer.timeout.connect(confirm)
    confirmer.start(100)
    try:
        wait_until(lambda: dialog.table.rowCount() == 2)
        dialog.table.selectRow(0)
        dialog.recompile_selected.click()
        conflict = finished_document(tasks, dialog)
        assert conflict.failed == 1 and conflict.started_at is None, conflict
        assert (kb / "wiki/concepts/after-confirmation.md").read_text(
            encoding="utf-8"
        ) == "手工编辑。"
        stale_confirmation = False
        dialog.table.selectRow(0)
        dialog.recompile_selected.click()
        selected = finished_document(tasks, dialog)
        assert selected.succeeded == 1 and selected.processes_reaped, selected
        assert str(kb / "wiki/summaries/native-short.md") in selected.results[0].resources
        dialog.recompile_all.click()
        all_docs = finished_document(tasks, dialog)
        assert (all_docs.succeeded, all_docs.skipped, all_docs.failed) == (2, 0, 0), all_docs
        assert "Summary" in (kb / "wiki/summaries/native-long.md").read_text(encoding="utf-8")
        assert not (kb / ".ultrakb/condb.sqlite").exists()
        assert "已有的来源正文" not in str(all_docs.summary())
    finally:
        confirmer.stop()
        dialog.reject()
