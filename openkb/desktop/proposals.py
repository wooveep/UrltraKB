"""Durable differences: loading and accepting use the common application API."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.application.proposals import list_proposals
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import AcceptProposal


class ProposalsDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self.scope = window.scope
        self._closed, self._task = False, None
        self._generation = 0
        self.setWindowTitle("待接受知识差异")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取保存的建议…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.selected)
        layout.addWidget(self.list)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        layout.addWidget(self.diff, 1)
        actions = QHBoxLayout()
        self.refresh_button, self.accept_button = (
            QPushButton("刷新差异"),
            QPushButton("接受所示全部差异"),
        )
        self.refresh_button.clicked.connect(self.reload)
        self.accept_button.clicked.connect(self.accept_selected)
        self.accept_button.setEnabled(False)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.accept_button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def reload(self):
        self._generation += 1
        generation = self._generation
        self.list.clear()
        self.accept_button.setEnabled(False)

        def loaded(items, error):
            if self._closed or generation != self._generation:
                return
            if error:
                self.status.setText(f"无法读取建议（{type(error).__name__}）")
                return
            for view in items:
                item = QListWidgetItem(f"{view.proposal_id} · {view.view_id} · {view.status}")
                item.setData(Qt.ItemDataRole.UserRole, view)
                self.list.addItem(item)
            self.status.setText(f"{len(items)} 项待处理建议。接受前请阅读全部差异。")

        self.window.io.submit(
            lambda: list_proposals(self.kb, scope=self.scope),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def selected(self, item, previous=None):
        self.diff.setPlainText(item.data(Qt.ItemDataRole.UserRole).diff if item else "")
        self.accept_button.setEnabled(item is not None and self._task is None)

    def accept_selected(self):
        item = self.list.currentItem()
        if item is None or self._task:
            return
        view = item.data(Qt.ItemDataRole.UserRole)
        self._task = self.window.manager.submit(
            self.kb, [AcceptProposal(view.proposal_id, view.version, view_id=view.view_id)]
        )
        self.accept_button.setEnabled(False)
        self.status.setText("正在核对当前正文和输入后发布…")

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        self.status.setText("\n".join(result.error or result.status for result in task.results))
        self.accept_button.setEnabled(False)

    def closeEvent(self, event):
        self._closed = True
        self.timer.stop()
        super().closeEvent(event)

    def reject(self):
        self._closed = True
        self.timer.stop()
        super().reject()
