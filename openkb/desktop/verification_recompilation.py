"""Opt-in native selection/confirmation and real spawned recompilation."""

from __future__ import annotations


def verify_recompilation(window, kb, wait_until):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.desktop.documents import DocumentsDialog
    from openkb.desktop.verification_tasks import SubmittedTasks, finished_document
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_json(
            kb / ".openkb/hashes.json",
            {
                "native-short": {"name": "短资料.md", "doc_name": "native-short", "type": "md"},
                "native-missing": {
                    "name": "来源已缺失.md",
                    "doc_name": "native-missing",
                    "type": "md",
                },
                "native-long": {
                    "name": "长资料.pdf",
                    "doc_name": "native-long",
                    "type": "long_pdf",
                    "doc_id": "doc-native-long",
                },
            },
        )
        atomic_write_text(kb / "wiki/sources/native-short.md", "# 原生重编译\n已有的来源正文。")
        atomic_write_text(
            kb / "wiki/summaries/native-long.md",
            "---\nsources: [raw/长资料.pdf]\n---\n# 既有长文摘要\n",
        )
    dialog = DocumentsDialog(window, kb)
    dialog.show()
    confirmer = QTimer()
    stale_confirmation = True
    tasks = SubmittedTasks(window.manager, wait_until)

    def confirm():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox) and modal.windowTitle() == "确认重编译":
            if stale_confirmation:
                with kb_ingest_lock(kb / ".openkb"):
                    atomic_write_text(kb / "wiki/concepts/after-confirmation.md", "手工编辑。")
            modal.done(QMessageBox.StandardButton.Yes)

    confirmer.timeout.connect(confirm)
    confirmer.start(100)
    try:
        wait_until(lambda: dialog.table.rowCount() == 3)
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
        assert (all_docs.succeeded, all_docs.skipped, all_docs.failed) == (2, 1, 0), all_docs
        assert "Summary" in (kb / "wiki/summaries/native-long.md").read_text(encoding="utf-8")
        assert not (kb / ".openkb/pageindex.db").exists()
        assert "已有的来源正文" not in str(all_docs.summary())
    finally:
        confirmer.stop()
        dialog.reject()
