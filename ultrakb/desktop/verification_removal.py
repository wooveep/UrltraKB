"""Real Qt preview, stale confirmation, spawned cleanup and retained resources."""

from __future__ import annotations


def verify_removal(window, kb, wait_until):
    from ultrakb.desktop.documents import DocumentsDialog
    from ultrakb.desktop.verification_data import publish_pages, retain_document
    from ultrakb.desktop.verification_tasks import SubmittedTasks, finished_document

    retain_document(
        kb,
        "待删除资料.md",
        "native-removal",
        "保留原文。\n",
        summary="# 摘要\n",
    )
    from ultrakb.desktop.verification_workbench import management_page

    dialog = management_page(window, kb, "资料", DocumentsDialog, wait_until)
    tasks = SubmittedTasks(window.manager, wait_until)
    try:
        wait_until(lambda: dialog.table.rowCount() == 1)
        dialog.table.selectRow(0)
        dialog.keep_raw.setChecked(True)
        dialog.keep_empty.setChecked(True)
        dialog.preview_button.click()
        wait_until(lambda: dialog.confirm_button.isEnabled())
        assert "待删除资料.md" in dialog.details.toPlainText()
        publish_pages(
            kb,
            {
                "concepts/native-removal.md": (
                    "---\nsources: [summaries/native-removal.md]\n---\n保留页面。\n"
                ).encode(),
            },
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
