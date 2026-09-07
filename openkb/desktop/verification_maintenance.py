"""Opt-in native check, overwrite consent and damaged-KB recovery acceptance."""


def verify_maintenance(window, kb, wait_until):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.desktop.maintenance import MaintenanceDialog
    from openkb.locks import atomic_write_text, kb_ingest_lock

    page = kb / "wiki/concepts/native-repair.md"
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(page, "# Native repair\n[[concepts/missing-native-target]]\n")
    dialog = MaintenanceDialog(window, kb)
    dialog.show()
    dialog.semantic.setChecked(False)
    stale = True
    confirmer = QTimer()

    def confirm():
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox) and modal.windowTitle() == "确认修复链接":
            if stale:
                with kb_ingest_lock(kb / ".openkb"):
                    atomic_write_text(page, page.read_text(encoding="utf-8") + "手工编辑。\n")
            modal.done(QMessageBox.StandardButton.Yes)

    def finish():
        wait_until(lambda: dialog._task is not None)
        task_id = dialog._task
        wait_until(lambda: dialog._task is None)
        return window.manager.get(task_id)

    confirmer.timeout.connect(confirm)
    confirmer.start(100)
    try:
        dialog.fix_button.click()
        conflict = finish()
        assert conflict.failed == 1 and conflict.started_at is None, conflict
        assert "[[concepts/missing-native-target]]" in page.read_text(encoding="utf-8")
        stale = False
        dialog.fix_button.click()
        fixed = finish()
        assert fixed.state == "completed" and fixed.processes_reaped, fixed
        assert "[[concepts/missing-native-target]]" not in page.read_text(encoding="utf-8")
        assert "手工编辑" in page.read_text(encoding="utf-8")
        assert "updated: wiki/concepts/native-repair.md" in fixed.results[0].changes
        first_report = fixed.results[0].resources[0]
        wait_until(lambda: "Structural Lint Report" in dialog.details.toPlainText())
        dialog.grab().save(str(kb.parent / "native-maintenance.png"))
        dialog.check_button.click()
        checked = finish()
        assert checked.state == "completed" and checked.results[0].resources[0] != first_report
    finally:
        confirmer.stop()
        dialog.reject()


def verify_diagnostics(window, kb, wait_until):
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.desktop.diagnostics import DiagnosticsDialog
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
    from openkb.mutation import repair_marker, snapshot_paths

    initialize_kb(kb, seed_environment=False)
    page = kb / "wiki/concepts/retained.md"
    original = "---\ntype: Concept\ndescription: Retained\n---\n# Retained original"
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(page, original)
        snapshot = snapshot_paths(kb, [page], operation="verification-interruption")
        atomic_write_text(page, "# Uncommitted body")
        atomic_write_json(repair_marker(kb), {"journal": snapshot.journal_path.name})
    backup = snapshot.entries[page]
    temporarily_missing = backup.with_suffix(".retained")
    backup.replace(temporarily_missing)
    dialog = DiagnosticsDialog(window, kb)
    dialog.show()
    try:
        wait_until(lambda: dialog.pages.findText("concepts/retained.md") >= 0)
        dialog.pages.setCurrentText("concepts/retained.md")
        dialog.read_page()
        wait_until(lambda: dialog.source.toPlainText() == "# Uncommitted body")
        dialog.grab().save(str(kb.parent / "native-diagnostics.png"))
        assert snapshot.journal_path.exists() and repair_marker(kb).exists()
        dialog.repair_button.click()
        wait_until(lambda: not dialog._repairing)
        assert not dialog.open_button.isEnabled()
        assert snapshot.journal_path.exists() and repair_marker(kb).exists()
        assert page.read_text(encoding="utf-8") == "# Uncommitted body"
        temporarily_missing.replace(backup)
        dialog.repair_button.click()
        wait_until(lambda: not dialog._repairing)
        assert dialog.open_button.isEnabled(), dialog.report.toPlainText()
        assert page.read_text(encoding="utf-8") == original
        assert not repair_marker(kb).exists() and not snapshot.journal_path.exists()
    finally:
        dialog.reject()


def verify_semantic_maintenance(window, kb, wait_until):
    from openkb.desktop.maintenance import MaintenanceDialog

    dialog = MaintenanceDialog(window, kb)
    dialog.show()
    try:
        dialog.check_button.click()
        task_id = dialog._task
        wait_until(lambda: dialog._task is None)
        task = window.manager.get(task_id)
        assert task.succeeded == 1 and not task.results[0].quality, task
        assert task.results[0].resources and task.processes_reaped
        wait_until(lambda: "Semantic" in dialog.details.toPlainText())
    finally:
        dialog.reject()
