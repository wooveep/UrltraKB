"""Native artifact generation, reading, complete export, and explicit HTML preview."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openkb import frontmatter
from openkb.application.artifacts import (
    artifact_files,
    export_artifact,
    list_artifacts,
    read_artifact,
)
from openkb.application.generators import GenerationOptions, preview_generation
from openkb.desktop.reader import MarkdownView
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import GenerateArtifact, GenerateGraph


class ArtifactsDialog(QDialog):
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
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(str(kb)))
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("Skill", "skill")
        self.kind.addItem("HTML 幻灯片", "deck")
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如 attention-guide")
        self.intent = QPlainTextEdit()
        self.intent.setPlaceholderText("希望产物说明什么、面向谁、如何使用…")
        self.intent.setMaximumHeight(85)
        form.addRow("类型", self.kind)
        form.addRow("名称", self.name)
        form.addRow("生成要求", self.intent)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.generate_button = QPushButton("生成…")
        self.graph_button = QPushButton("生成知识图谱")
        refresh = QPushButton("刷新产物")
        for button, callback in (
            (self.generate_button, self.generate),
            (self.graph_button, self.graph),
            (refresh, self.refresh),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("同名产物可修改名称，或确认归档旧版后替换。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.items = QListWidget()
        self.items.currentItemChanged.connect(self.select)
        self.files = QComboBox()
        self.files.currentIndexChanged.connect(self.read)
        self.reader = MarkdownView()
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.reader, "阅读")
        self.tabs.addTab(self.source, "源文件")
        self.tabs.addTab(self.results, "执行结果")
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addWidget(self.files)
        content_layout.addWidget(self.tabs)
        splitter = QSplitter()
        splitter.addWidget(self.items)
        splitter.addWidget(content)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        outputs = QHBoxLayout()
        self.export_button = QPushButton("导出所选产物…")
        self.export_button.clicked.connect(self.export)
        self.preview_button = QPushButton("在外部浏览器预览 HTML")
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview)
        outputs.addWidget(self.export_button)
        outputs.addWidget(self.preview_button)
        layout.addLayout(outputs)
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
        self.status.setText("正在读取现有产物与覆盖范围…")

        def loaded(preview, error):
            self._preparing = False
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
            self.status.setText("生成任务已提交。要求会保留在表单中；可在主窗口安全停止任务。")

        self.window.io.submit(
            lambda: preview_generation(self.kb, kind, name),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def graph(self):
        self._tasks.add(self.window.manager.submit(self.kb, [GenerateGraph()]))
        self.status.setText("图谱任务已提交，将更新 output/visualize/graph.html。")

    def refresh(self):
        self._refresh_id += 1
        request_id = self._refresh_id

        def loaded(items, error):
            if error:
                self.status.setText(f"无法读取产物（{type(error).__name__}）")
                return
            selected = self.items.currentItem()
            selected_path = selected.data(Qt.ItemDataRole.UserRole) if selected else None
            self.items.clear()
            for artifact in items:
                item = QListWidgetItem(f"{artifact.kind} · {artifact.path}", self.items)
                item.setData(Qt.ItemDataRole.UserRole, artifact.path)
                if artifact.path == selected_path:
                    self.items.setCurrentItem(item)

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
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)

        def loaded(files, error):
            if error:
                self.status.setText(f"无法读取产物文件（{type(error).__name__}）")
                return
            self.files.addItems(files)

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
                    "HTML 可通过下方按钮在外部浏览器预览。其他文件可查看源文件或导出。"
                )
                self.tabs.setCurrentIndex(1)

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
        self.status.setText("任务已结束。请查看结果；生成要求已保留，可修改名称或重新确认后再试。")
        self.results.setPlainText("\n".join(lines))
        self.tabs.setCurrentIndex(2)
        self.refresh()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        self.reader.stop_rendering()
        super().done(result)
