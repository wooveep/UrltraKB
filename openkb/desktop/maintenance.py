"""Native knowledge checks and version-bound link repair."""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.application.maintenance import preview_link_repair
from openkb.application.pages import read_page
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import CheckKnowledge


class MaintenanceDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self._task = None
        self._preparing = False
        self._report = None
        self.setWindowTitle(f"检查与修复 · {kb.name}")
        self.resize(880, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(str(kb)))
        self.semantic = QCheckBox("同时运行语义检查（使用模型）")
        self.semantic.setChecked(True)
        layout.addWidget(self.semantic)
        actions = QHBoxLayout()
        self.check_button = QPushButton("检查并保存报告")
        self.fix_button = QPushButton("修复链接并检查…")
        self.open_button = QPushButton("打开报告")
        self.open_button.setEnabled(False)
        for button, callback in (
            (self.check_button, lambda: self.start(fix=False)),
            (self.fix_button, lambda: self.start(fix=True)),
            (self.open_button, self.open_report),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("结构检查会报告链接、孤立页、索引与页面元数据问题。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)

    def start(self, *, fix):
        if self._task or self._preparing:
            return
        self._report = None
        self.open_button.setEnabled(False)
        self.details.clear()
        semantic = self.semantic.isChecked()
        if not fix:
            self._task = self.window.manager.submit(self.kb, [CheckKnowledge(semantic=semantic)])
            self.status.setText("检查任务已提交。可在主窗口查看状态或安全停止。")
            return
        self._preparing = True
        self.status.setText("正在读取最新结构与链接修复范围…")

        def loaded(preview, error):
            self._preparing = False
            if error:
                self.status.setText(f"无法准备链接修复（{type(error).__name__}）")
                return
            version, report = preview
            question = QMessageBox(self)
            question.setWindowTitle("确认修复链接")
            question.setText("规范化可匹配的知识链接，并把无法匹配的链接改为普通文字？")
            question.setInformativeText(
                "会修改相关页面正文。修复完成后会保留结果；后续检查失败或停止任务不会撤销修复。"
            )
            question.setDetailedText(report)
            question.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            question.setDefaultButton(QMessageBox.StandardButton.No)
            if question.exec() == QMessageBox.StandardButton.Yes:
                self._task = self.window.manager.submit(
                    self.kb, [CheckKnowledge(semantic=semantic, fix=True, version=version)]
                )
                self.status.setText("修复与检查任务已提交。")

        self.window.io.submit(
            lambda: preview_link_repair(self.kb),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        lines = []
        for result in task.results:
            lines.extend(result.changes)
            lines.extend(f"质量提示：{issue}" for issue in result.quality)
            lines.extend(f"未完成：{stage}" for stage in result.unfinished)
            if result.error:
                lines.append(result.error)
            if result.resources:
                self._report = Path(result.resources[0])
        if task.error:
            lines.append(task.error)
        self.details.setPlainText("\n".join(lines))
        incomplete = any(result.unfinished for result in task.results)
        self.status.setText(
            "已保留执行成果，但有未完成的检查。请查看结果与报告。"
            if incomplete
            else "检查结束，请查看报告中的问题。"
            if task.succeeded
            else "本次检查未完成，请查看结果。"
        )
        self.open_button.setEnabled(self._report is not None)
        if self._report is not None:
            report = self._report

            def loaded(page, error):
                if error is None:
                    self.details.appendPlainText("\n" + page.body)
                    self.details.verticalScrollBar().setValue(0)

            self.window.io.submit(
                lambda: read_page(self.kb, str(report.relative_to(self.kb / "wiki"))),
                loaded,
                kb=self.kb,
                obsolete=lambda: self._closed or self._task is not None or self._report != report,
            )

    def open_report(self):
        if self._report and self.window.kb == self.kb:
            self.window.open_page(str(self._report.relative_to(self.kb / "wiki")))
            self.accept()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
