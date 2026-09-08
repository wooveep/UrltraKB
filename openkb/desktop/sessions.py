"""Native completed-conversation inventory, export and confirmed deletion."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
)

from openkb.agent.chat_session import list_sessions
from openkb.application.conversations import read_conversation
from openkb.desktop.panels import ManagementPanel
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import DeleteConversation, ExportConversation


class SessionsDialog(ManagementPanel):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self._generation = 0
        self._task = None
        self.setWindowTitle(f"对话管理 · {kb.name}")
        self.resize(780, 540)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("自动保存的对话，点击即可继续。"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["对话", "完整回合", "最近更新"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.invalidate)
        self.table.cellClicked.connect(lambda *_: self.open())
        self.table.itemActivated.connect(lambda *_: self.open())
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.reload)
        actions.addWidget(refresh)
        actions.addStretch()
        more = QToolButton()
        more.setText("更多")
        more.setAccessibleName("对话历史操作")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("导出 Markdown 副本", self.export)
        menu.addAction("删除对话…", self.delete)
        more.setMenu(menu)
        actions.addWidget(more)
        layout.addLayout(actions)
        self.status = QLabel("正在读取完整对话…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(110)
        self.details.hide()
        layout.addWidget(self.details)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def invalidate(self):
        self._generation += 1

    def selected(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None

    def reload(self, *, preserve_result=False):
        self.invalidate()
        generation = self._generation

        def loaded(value, error):
            if error:
                self.status.setText(f"读取对话失败（{type(error).__name__}）")
                return
            if not preserve_result:
                self.status.setText("点击对话即可继续。" if value else "暂无已保存的对话。")
            self.table.setRowCount(len(value))
            for row, session in enumerate(value):
                item = QTableWidgetItem(session["title"] or session["id"])
                item.setData(Qt.ItemDataRole.UserRole, session["id"])
                self.table.setItem(row, 0, item)
                self.table.setItem(row, 1, QTableWidgetItem(str(session["turn_count"])))
                self.table.setItem(row, 2, QTableWidgetItem(session["updated_at"][:10]))

        self.window.io.submit(
            lambda: list_sessions(self.kb),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def open(self):
        identity = self.selected()
        if not identity or self.window.kb != self.kb:
            return
        self.window._load_conversation(identity)
        if not getattr(self, "embedded", False):
            self.accept()

    def export(self):
        identity = self.selected()
        if identity and not self._task:
            self._task = self.window.manager.submit(self.kb, [ExportConversation(identity)])
            self.status.setText("已提交导出；完整历史将保存为新的 Markdown 副本。")

    def delete(self):
        identity = self.selected()
        if not identity or self._task:
            return
        self.invalidate()
        generation = self._generation

        def loaded(session, error):
            if error:
                self.status.setText(f"无法确认对话（{type(error).__name__}），请刷新。")
                return
            question = QMessageBox(self)
            question.setWindowTitle("确认删除对话")
            question.setText(
                f"删除“{session.title or session.id}”及其 {len(session.turns)} 个完整回合？"
            )
            question.setInformativeText("已导出的副本会保留。若等待期间对话有更新，需要重新确认。")
            question.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            question.setDefaultButton(QMessageBox.StandardButton.No)
            if question.exec() == QMessageBox.StandardButton.Yes:
                self._task = self.window.manager.submit(
                    self.kb, [DeleteConversation(identity, session.version)]
                )
                self.status.setText("已提交删除，可在任务面板查看结果或安全停止。")

        self.window.io.submit(
            lambda: read_conversation(self.kb, identity),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        lines = [line for result in task.results for line in (*result.changes, *result.resources)]
        lines.extend(result.error for result in task.results if result.error)
        if task.error:
            lines.append(task.error)
        self.details.setPlainText("\n".join(lines))
        self.details.setVisible(bool(lines))
        self.status.setText(
            "会话已不存在，本项已跳过。"
            if task.skipped
            else "任务完成。"
            if task.state == "completed"
            else "任务未完成，请查看结果。"
        )
        self.reload(preserve_result=True)
        if self.window.kb == self.kb:
            self.window._refresh_current()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
