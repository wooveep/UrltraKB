"""Native artifact forms, committed exports, conflict consent and real SDK tools."""


def verify_artifacts(window, kb, wait_until, *, model=False):
    import zipfile

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.application.artifacts import export_artifact
    from openkb.desktop.artifacts import ArtifactsDialog
    from openkb.desktop.verification_tasks import SubmittedTasks, assert_result_text
    from openkb.locks import atomic_write_text, kb_ingest_lock

    reading_path = "output/skills/native-reading/SKILL.md"
    reading_source = (
        "---\nname: native-reading\ndescription: 元数据应保留在源文件\n---\n\n"
        "# 原生 Skill 正文\n\n阅读时直接显示操作说明。\n"
    )
    delimiter_cases = {
        "opening.md": "---Warning: keep backups\n---\n\n# 保留正文\n",
        "closing.md": "---\nWarning: keep backups\n---not-a-delimiter\n\n# 保留正文\n",
    }
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(
            kb / "wiki/concepts/产物验证.md", "# 产物验证\n\n可供生成器引用的原生验收知识。"
        )
        atomic_write_text(kb / reading_path, reading_source)
        for name, source in delimiter_cases.items():
            atomic_write_text(kb / "output/skills/native-reading" / name, source)
    from openkb.desktop.verification_workbench import management_page

    dialog = management_page(window, kb, "产物", ArtifactsDialog, wait_until)
    tasks = SubmittedTasks(window.manager, wait_until)

    def finish():
        task = tasks.finish(lambda: not dialog._tasks)
        assert "任务已结束" in dialog.status.text(), dialog.status.text()
        assert f"任务结果：{task.state}" in dialog.results.toPlainText()
        assert_result_text(task, dialog.results.toPlainText())
        return task

    try:
        wait_until(lambda: dialog.items.count() > 0)
        for i in range(dialog.items.count()):
            item = dialog.items.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == "output/skills/native-reading":
                dialog.items.setCurrentItem(item)
                break
        wait_until(lambda: dialog.files.findText(reading_path) >= 0)
        dialog.files.setCurrentText(reading_path)
        wait_until(lambda: "原生 Skill 正文" in dialog.reader.toPlainText())
        assert dialog.reader.toPlainText().strip().startswith("原生 Skill 正文")
        assert "元数据应保留在源文件" not in dialog.reader.toPlainText()
        assert dialog.source.toPlainText() == reading_source
        assert (kb / reading_path).read_text(encoding="utf-8") == reading_source
        dialog.grab().save(str(kb.parent / "native-artifact-reading.png"))
        for name, source in delimiter_cases.items():
            dialog.files.setCurrentText(f"output/skills/native-reading/{name}")
            wait_until(lambda: dialog.source.toPlainText() == source)
            assert "Warning: keep backups" in dialog.reader.toPlainText()
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
