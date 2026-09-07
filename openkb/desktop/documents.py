"""Native document inventory and version-bound removal confirmation."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openkb.application.knowledge_bases import get_kb_list
from openkb.application.removal import preview_removal
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import RemoveDocument


class DocumentsDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self._generation = 0
        self._confirmed = None
        self._task = None
        self.setWindowTitle(f"资料管理 · {kb.name}")
        self.resize(880, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(str(kb)))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["资料", "类型"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.invalidate)
        layout.addWidget(self.table)
        options = QHBoxLayout()
        self.keep_raw = QCheckBox("保留原文")
        self.keep_empty = QCheckBox("保留失去全部来源的概念 / 实体页面")
        for checkbox in (self.keep_raw, self.keep_empty):
            checkbox.toggled.connect(self.invalidate)
            options.addWidget(checkbox)
        layout.addLayout(options)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("刷新资料")
        self.preview_button = QPushButton("查看删除计划")
        self.confirm_button = QPushButton("确认删除")
        self.confirm_button.setEnabled(False)
        for button, callback in (
            (self.refresh_button, self.reload),
            (self.preview_button, self.preview),
            (self.confirm_button, self.confirm),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("正在读取资料…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def invalidate(self):
        self._generation += 1
        self._confirmed = None
        self.confirm_button.setEnabled(False)

    def reload(self):
        self.invalidate()

        def loaded(value, error):
            if self._closed:
                return
            if error:
                self.status.setText(f"读取资料失败（{type(error).__name__}）")
                return
            self.table.setRowCount(len(value["documents"]))
            for row, doc in enumerate(value["documents"]):
                item = QTableWidgetItem(doc["name"])
                item.setData(Qt.ItemDataRole.UserRole, doc["hash"])
                self.table.setItem(row, 0, item)
                self.table.setItem(row, 1, QTableWidgetItem(doc.get("display_type", doc["type"])))

        self.window.io.submit(
            lambda: get_kb_list(self.kb), loaded, kb=self.kb, obsolete=lambda: self._closed
        )

    def preview(self):
        row = self.table.currentRow()
        if row < 0 or self._task:
            return
        identifier = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        keep_raw, keep_empty = self.keep_raw.isChecked(), self.keep_empty.isChecked()
        self.invalidate()
        generation = self._generation
        self.status.setText("正在读取最新删除计划…")

        def loaded(value, error):
            if self._closed or generation != self._generation:
                return
            if error:
                self.status.setText(f"计划读取失败（{type(error).__name__}）")
                return
            if value.status != "ready":
                self.status.setText("资料已变化，请刷新资料列表。")
                return
            self._confirmed = RemoveDocument(identifier, value.version, keep_raw, keep_empty)
            self.details.setPlainText("\n".join(f"{a.tag}  {a.target}" for a in value.plan.actions))
            self.status.setText("确认后执行以上清理。若知识库已变化，将要求重新查看并确认计划。")
            self.confirm_button.setEnabled(True)

        self.window.io.submit(
            lambda: preview_removal(self.kb, identifier, keep_raw=keep_raw, keep_empty=keep_empty),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def confirm(self):
        if self._confirmed is None or self._task:
            return
        self._task = self.window.manager.submit(self.kb, [self._confirmed])
        self.invalidate()
        self.status.setText("删除任务已提交，可在主窗口查看状态或安全停止。")

    def poll(self):
        if self._task is None:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        lines = []
        for result in task.results:
            if result.error:
                lines.append(result.error)
            lines.extend(result.changes)
            lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
            lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        if task.error and not lines:
            lines.append(task.error)
        self.details.setPlainText("\n".join(lines))
        self.status.setText(
            "清理完成。"
            if task.state == "completed"
            else "清理未全部完成。请查看结果；重新查看计划后可明确重试。"
        )
        self.reload()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
