"""Native artifact generation, reading, complete export, and explicit HTML preview."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QListWidgetItem,
    QMessageBox,
)

from openkb import frontmatter
from openkb.application.artifacts import (
    artifact_files,
    export_artifact,
    list_artifacts,
    read_artifact,
)
from openkb.application.generators import GenerationOptions, preview_generation
from openkb.desktop.panels import ManagementPanel
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import GenerateArtifact, GenerateGraph


class ArtifactsDialog(ManagementPanel):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self._preparing = False
        self._tasks = set()
        self._selection = 0
        self._refresh_id = 0
        self.setWindowTitle(f"生成与产物 · {kb.name}")
        self.resize(1080, 800)
        from openkb.desktop.artifact_workspace import build_workspace

        self._preferred_path = None
        build_workspace(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.refresh()

    def generate(self):
        if self._preparing:
            return
        kind, name, intent = (
            self.kind.currentData(),
            self.name.text().strip(),
            self.intent.toPlainText(),
        )
        try:
            GenerationOptions(kind, name, intent)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._preparing = True
        self.generate_button.setEnabled(False)
        self.status.setText("正在读取现有产物与覆盖范围…")

        def loaded(preview, error):
            self._preparing = False
            self.generate_button.setEnabled(True)
            if error:
                self.status.setText(f"无法准备生成（{type(error).__name__}）")
                return
            if preview.exists:
                question = QMessageBox(self)
                question.setWindowTitle("确认归档并替换产物")
                question.setText(f"归档并替换 {preview.target.relative_to(self.kb)}？")
                question.setInformativeText(
                    "现有目录会完整保存在相邻的历史目录。生成失败时会保留归档和已提交的部分文件。"
                    "若希望另存，请取消并修改名称。"
                )
                question.setStandardButtons(
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                question.setDefaultButton(QMessageBox.StandardButton.No)
                if question.exec() != QMessageBox.StandardButton.Yes:
                    self.status.setText("已取消。可以修改名称后再生成。")
                    return
            task = self.window.manager.submit(
                self.kb,
                [
                    GenerateArtifact(
                        kind,
                        name,
                        intent,
                        preview.version,
                        replace=preview.exists,
                    )
                ],
            )
            self._tasks.add(task)
            self._preferred_path = f"output/{'skills' if kind == 'skill' else 'decks'}/{name}"
            self.new_button.setChecked(False)
            self.status.setText("正在生成成果，可继续阅读已有成果。在「任务」中查看进度或停止。")

        self.window.io.submit(
            lambda: preview_generation(self.kb, kind, name),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def graph(self):
        self._tasks.add(self.window.manager.submit(self.kb, [GenerateGraph()]))
        self._preferred_path = "output/visualize/graph.html"
        self.status.setText("正在整理知识之间的关联，完成后可预览知识图谱。")

    def describe_kind(self):
        self.kind_hint.setText(
            "将知识整理为 AI 可复用的操作指南，包含步骤与参考资料。"
            if self.kind.currentData() == "skill"
            else "围绕一个主题生成用于讲解、培训或分享的演示文稿，可在浏览器播放。"
        )

    def filter_items(self):
        query, kind = self.search.text().strip().casefold(), self.filter_kind.currentText()
        count = 0
        for index in range(self.items.count()):
            item = self.items.item(index)
            visible = query in item.text().casefold() and (
                kind == "全部成果" or item.data(Qt.ItemDataRole.UserRole + 1) == kind
            )
            item.setHidden(not visible)
            count += visible
        self.library_count.setText(f"我的成果 · {count}")
        self.empty_hint.setVisible(count == 0)
        self.empty_hint.setText(
            "没有匹配的成果，试试其他条件。"
            if self.items.count()
            else "还没有成果。点击「新建创作」，把知识变成可用的成果。"
        )
        selected = self.items.currentItem()
        if selected and selected.isHidden():
            self.items.setCurrentItem(
                next(
                    (
                        self.items.item(i)
                        for i in range(self.items.count())
                        if not self.items.item(i).isHidden()
                    ),
                    None,
                )
            )

    def refresh(self):
        self._refresh_id += 1
        request_id = self._refresh_id

        def loaded(items, error):
            if error:
                self.status.setText(f"无法读取产物（{type(error).__name__}）")
                return
            selected = self.items.currentItem()
            selected_path = self._preferred_path or (
                selected.data(Qt.ItemDataRole.UserRole) if selected else None
            )
            blocked = self.items.blockSignals(True)
            self.items.clear()
            for artifact in items:
                title = "知识关联图谱" if artifact.kind == "知识图谱" else Path(artifact.path).stem
                item = QListWidgetItem(f"{title}\n{artifact.kind}", self.items)
                item.setData(Qt.ItemDataRole.UserRole, artifact.path)
                item.setData(Qt.ItemDataRole.UserRole + 1, artifact.kind)
                item.setToolTip(artifact.path)
                if artifact.path == selected_path:
                    self.items.setCurrentItem(item)
                    self._preferred_path = None
            if self.items.currentItem() is None and self.items.count():
                self.items.setCurrentRow(0)
            self.filter_items()
            self.items.blockSignals(blocked)
            self.select(self.items.currentItem())

        self.window.io.submit(
            lambda: list_artifacts(self.kb),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or request_id != self._refresh_id,
        )

    def select(self, item, previous=None):
        self._selection += 1
        selection = self._selection
        self.files.clear()
        self.export_button.setEnabled(bool(item))
        if not item:
            self.files.hide()
            self.reader.show_temporary(
                "从左侧选择已有成果进行阅读、预览与导出。\n\n还没有成果？点击「新建创作」，选择目标并描述要求。"
            )
            self.artifact_title.setText("选择一个成果")
            self.artifact_hint.setText("从左侧选择成果进行预览，或新建一次创作。")
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.artifact_title.setText(item.text().splitlines()[0])
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        self.artifact_hint.setText(
            {
                "Skill": "阅读操作指南；导出完整文件包后，可交给支持 Skill 的 AI 工具使用。",
                "幻灯片": "在浏览器中播放演示文稿；导出会包含展示所需的全部文件。",
                "知识图谱": "在浏览器中探索知识之间的关联，也可以导出分享。",
                "检查报告": "查看知识质量检查的发现，再到知识页修订内容。",
            }.get(kind, "阅读成果，或导出文件用于后续工作。")
        )

        def loaded(files, error):
            if error:
                self.status.setText(f"无法读取产物文件（{type(error).__name__}）")
                return
            blocked = self.files.blockSignals(True)
            self.files.addItems(files)
            preferred = next(
                (
                    file
                    for file in files
                    if Path(file).name in {"SKILL.md", "index.html", "graph.html"}
                ),
                None,
            )
            if preferred:
                self.files.setCurrentText(preferred)
            self.files.blockSignals(blocked)
            self.files.setVisible(len(files) > 1)
            self.read()

        self.window.io.submit(
            lambda: artifact_files(self.kb, path),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or selection != self._selection,
        )

    def read(self):
        relative = self.files.currentText()
        html = Path(relative).suffix.lower() in {".html", ".htm"}
        self.preview_button.setEnabled(bool(relative) and html)
        if not relative:
            self.source.clear()
            self.reader.show_temporary("选择产物及其文件以查看。")
            return

        def loaded(text, error):
            if error:
                self.source.setPlainText(
                    f"此文件无法作为 UTF-8 文本读取（{type(error).__name__}）。可导出完整产物。"
                )
                self.reader.show_temporary("请导出后使用对应文件程序打开。")
                return
            self.source.setPlainText(text)
            if Path(relative).suffix.lower() == ".md":
                parts = frontmatter.split(text) if frontmatter.parse(text) else None
                if parts:
                    lines = parts[0].splitlines()
                    if lines[0] != "---" or lines[-1] != "---":
                        parts = None
                self.reader.show_markdown(parts[1] if parts else text, (self.kb / relative).parent)
                self.tabs.setCurrentIndex(0)
            else:
                self.reader.show_temporary(
                    "点击「在浏览器预览」查看完整效果。也可以导出成果用于分享。"
                    if html
                    else "此文件可通过「源文件」查看，或导出后使用对应程序打开。"
                )
                self.tabs.setCurrentIndex(0)

        self.window.io.submit(
            lambda: read_artifact(self.kb, relative),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or self.files.currentText() != relative,
        )

    def export(self):
        item = self.items.currentItem()
        if not item:
            return
        destination = QFileDialog.getExistingDirectory(self, "选择知识库以外的导出目录")
        if not destination:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.window.io.submit(
            lambda: export_artifact(self.kb, path, Path(destination)),
            lambda output, error: self.status.setText(
                f"导出未完成（{type(error).__name__}）" if error else f"已导出新副本：{output}"
            ),
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def preview(self):
        relative = self.files.currentText()
        if Path(relative).suffix.lower() in {".html", ".htm"}:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.kb / relative)))

    def poll(self):
        completed = [
            self.window.manager.get(task)
            for task in self._tasks
            if self.window.manager.get(task).state in TERMINAL
        ]
        if not completed:
            return
        lines = []
        for task in completed:
            self._tasks.discard(task.id)
            lines.append(f"任务结果：{task.state}")
            if task.error:
                lines.append(task.error)
            for result in task.results:
                lines.extend(result.changes)
                lines.extend(f"质量提示：{issue}" for issue in result.quality)
                lines.extend(f"未完成：{stage}" for stage in result.unfinished)
                if result.error:
                    lines.append(result.error)
                if result.output:
                    lines.append(result.output)
        failed = any(task.state not in {"completed"} for task in completed)
        self.status.setText(
            "任务已结束。请查看任务记录，调整要求后可再次生成。"
            if failed
            else "任务已结束，成果已就绪。可以预览或导出，也可以发起新的创作。"
        )
        self.results.setPlainText("\n".join(lines))
        if failed:
            self.tabs.setCurrentIndex(2)
        self.refresh()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        self.reader.stop_rendering()
        super().done(result)
