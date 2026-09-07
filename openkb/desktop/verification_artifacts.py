"""Native artifact forms, committed exports, conflict consent and real SDK tools."""


def verify_artifacts(window, kb, wait_until, *, model=False):
    import zipfile

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.application.artifacts import export_artifact
    from openkb.desktop.artifacts import ArtifactsDialog
    from openkb.locks import atomic_write_text, kb_ingest_lock

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(
            kb / "wiki/concepts/产物验证.md", "# 产物验证\n\n可供生成器引用的原生验收知识。"
        )
    dialog = ArtifactsDialog(window, kb)
    dialog.show()

    def finish():
        wait_until(lambda: bool(dialog._tasks))
        task_id = next(iter(dialog._tasks))
        wait_until(lambda: not dialog._tasks)
        task = window.manager.get(task_id)
        assert task.processes_reaped, task
        return task

    try:
        dialog.graph_button.click()
        task = finish()
        assert task.succeeded == 1, task
        assert (kb / "output/visualize/graph.html").is_file()
        if model:
            dialog.name.setText("native-skill")
            dialog.intent.setPlainText(
                "Create a reusable skill from this knowledge. 保留中文参考。"
            )
            dialog.generate_button.click()
            task = finish()
            assert task.succeeded == 1 and not task.results[0].quality, task
            skill = kb / "output/skills/native-skill"
            assert (skill / "SKILL.md").is_file()
            assert (skill / "references/support.md").is_file()
            stale = True
            confirmer = QTimer()

            def confirm():
                modal = QApplication.activeModalWidget()
                if isinstance(modal, QMessageBox) and modal.windowTitle() == "确认归档并替换产物":
                    if stale:
                        with kb_ingest_lock(kb / ".openkb"):
                            atomic_write_text(
                                skill / "references/support.md", "Changed after consent"
                            )
                    modal.done(QMessageBox.StandardButton.Yes)

            confirmer.timeout.connect(confirm)
            confirmer.start(100)
            try:
                dialog.generate_button.click()
                task = finish()
                assert task.failed == 1 and task.started_at is None, task
                assert "Artifacts changed" in dialog.results.toPlainText()
                stale = False
                dialog.generate_button.click()
                task = finish()
                assert task.succeeded == 1, task
                archive = skill.with_name("native-skill-workspace") / "iteration-1"
                assert (archive / "references/support.md").read_text() == "Changed after consent"
            finally:
                confirmer.stop()
            destination = kb.parent / "exports"
            destination.mkdir(exist_ok=True)
            first = export_artifact(kb, "output/skills/native-skill", destination)
            second = export_artifact(kb, "output/skills/native-skill", destination)
            assert first != second
            with zipfile.ZipFile(first) as archive:
                assert "native-skill/references/support.md" in archive.namelist()
            dialog.kind.setCurrentIndex(1)
            dialog.name.setText("native-deck")
            dialog.intent.setPlainText("Create eight slides from the knowledge.")
            dialog.generate_button.click()
            task = finish()
            assert task.succeeded == 1, task
            assert (kb / "output/decks/native-deck/index.html").is_file()
            dialog.refresh()
            wait_until(
                lambda: any(
                    dialog.items.item(i).data(Qt.ItemDataRole.UserRole)
                    == "output/decks/native-deck"
                    for i in range(dialog.items.count())
                )
            )
            for i in range(dialog.items.count()):
                item = dialog.items.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == "output/decks/native-deck":
                    dialog.items.setCurrentItem(item)
                    break
            wait_until(
                lambda: dialog.preview_button.isEnabled()
                and "<section" in dialog.source.toPlainText()
            )
        dialog.grab().save(str(kb.parent / "native-artifacts.png"))
    finally:
        dialog.reject()
        wait_until(dialog.reader.rendering_stopped)
