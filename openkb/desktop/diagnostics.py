"""Restricted page inspection and deliberate recovery for damaged knowledge bases."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
)

from openkb.application.repair import (
    inspect_knowledge_base,
    read_diagnostic_page,
    repair_knowledge_base,
)


class DiagnosticsDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb.resolve()
        self._closed = False
        self._generation = 0
        self._repairing = False
        self.setWindowTitle(f"受限诊断 · {kb.name}")
        self.resize(1000, 700)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            f"{kb}\n可查看并复制保留的页面。普通写入仍受恢复状态约束。"
            "尝试修复会恢复有效日志并检查存储；证据缺失时保留原文件与修复状态。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新诊断")
        self.repair_button = QPushButton("尝试受控修复")
        self.open_button = QPushButton("重新打开知识库")
        self.open_button.setEnabled(False)
        for button, callback in (
            (self.refresh_button, self.reload),
            (self.repair_button, self.repair),
            (self.open_button, self.reopen),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("正在读取诊断信息…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.pages = QComboBox()
        self.pages.activated.connect(self.read_page)
        layout.addWidget(self.pages)
        splitter = QSplitter()
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        splitter.addWidget(self.report)
        splitter.addWidget(self.source)
        layout.addWidget(splitter, 1)
        self.reload()

    def reload(self):
        if self._repairing:
            return
        self._generation += 1
        generation = self._generation

        def loaded(value, error):
            if error:
                self.status.setText(f"诊断读取失败（{type(error).__name__}）")
                return
            self.pages.clear()
            self.pages.addItems(value.pages)
            self.report.setPlainText(
                "\n".join([*(f"保留的恢复日志：{name}" for name in value.journals), *value.issues])
                + "\n\n"
                + (value.structural_report or "")
            )
            self.status.setText(
                "需要受控恢复；此窗口仅提供受限查看。"
                if value.needs_repair
                else "未发现待恢复日志；可尝试修复以确认存储状态。"
            )
            self.read_page()

        self.window.io.submit(
            lambda: inspect_knowledge_base(self.kb),
            loaded,
            kb=self.kb,
            repair=True,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def read_page(self):
        path = self.pages.currentText()
        if not path:
            self.source.clear()
            return

        def loaded(value, error):
            self.source.setPlainText(
                value if error is None else f"页面无法读取（{type(error).__name__}）"
            )

        self.window.io.submit(
            lambda: read_diagnostic_page(self.kb, path),
            loaded,
            kb=self.kb,
            repair=True,
            obsolete=lambda: self._closed or path != self.pages.currentText(),
        )

    def repair(self):
        if self._repairing:
            return
        self._repairing = True
        self._generation += 1
        self.status.setText("正在等待安全修复…")
        self.open_button.setEnabled(False)

        def loaded(result, error):
            self._repairing = False
            if error:
                self.status.setText(f"修复未完成（{type(error).__name__}），恢复证据已保留。")
                return
            self.status.setText(
                "恢复与存储检查通过，可以重新打开。"
                if result.repaired
                else "修复未完成，继续保留恢复证据与写入限制。"
            )
            self.report.setPlainText(
                "\n".join((*result.recovery, *result.issues))
                + "\n\n"
                + (result.structural_report or "")
            )
            self.open_button.setEnabled(result.repaired)

        self.window.io.submit(
            lambda: repair_knowledge_base(self.kb),
            loaded,
            kb=self.kb,
            repair=True,
            global_settings=True,
            obsolete=lambda: self._closed,
        )

    def reopen(self):
        self.window.open_knowledge_base(self.kb)
        self.accept()

    def done(self, result):
        self._closed = True
        super().done(result)
