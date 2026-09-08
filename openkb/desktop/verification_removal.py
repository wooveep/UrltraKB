"""Real Qt preview, stale confirmation, spawned cleanup and retained resources."""

from __future__ import annotations


def verify_removal(window, kb, wait_until):
    from openkb.desktop.documents import DocumentsDialog
    from openkb.desktop.verification_tasks import SubmittedTasks, finished_document
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_json(
            kb / ".openkb/hashes.json",
            {
                "native-removal": {
                    "name": "待删除资料.md",
                    "doc_name": "native-removal",
                    "type": "short",
                    "raw_path": "raw/待删除资料.md",
                }
            },
        )
        atomic_write_text(kb / "raw/待删除资料.md", "保留原文。\n")
        atomic_write_text(kb / "wiki/sources/native-removal.md", "资料正文。\n")
        atomic_write_text(kb / "wiki/summaries/native-removal.md", "# 摘要\n")
    dialog = DocumentsDialog(window, kb)
    dialog.show()
    tasks = SubmittedTasks(window.manager, wait_until)
    try:
        wait_until(lambda: dialog.table.rowCount() == 1)
        dialog.table.selectRow(0)
        dialog.keep_raw.setChecked(True)
        dialog.keep_empty.setChecked(True)
        dialog.preview_button.click()
        wait_until(lambda: dialog.confirm_button.isEnabled())
        assert "待删除资料.md" in dialog.details.toPlainText()
        with kb_ingest_lock(kb / ".openkb"):
            atomic_write_text(
                kb / "wiki/concepts/native-removal.md",
                "---\nsources: [summaries/native-removal.md]\n---\n保留页面。\n",
            )
        dialog.confirm_button.click()
        assert finished_document(tasks, dialog).failed == 1
        assert (kb / "wiki/summaries/native-removal.md").exists()
        dialog.table.selectRow(0)
        dialog.preview_button.click()
        wait_until(lambda: dialog.confirm_button.isEnabled())
        dialog.confirm_button.click()
        result = finished_document(tasks, dialog)
        assert result.state == "completed" and result.processes_reaped, result
        assert not (kb / "wiki/summaries/native-removal.md").exists()
        assert (kb / "raw/待删除资料.md").exists()
        assert (kb / "wiki/concepts/native-removal.md").exists()
        assert str(kb / "raw/待删除资料.md") in result.results[0].resources
        assert "deleted: wiki/summaries/native-removal.md" in result.results[0].changes
        assert "保留页面" not in str(result.summary())
    finally:
        dialog.reject()
