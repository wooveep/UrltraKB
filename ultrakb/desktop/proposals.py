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

from ultrakb.application.proposals import list_proposal_page, read_proposal
from ultrakb.runtime.records import TERMINAL
from ultrakb.runtime.requests import AcceptProposal


class ProposalsDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        captured = getattr(window, "read_scope", None)
        self.scope = (
            captured if captured is not None and captured.knowledge is not None else window.scope
        )
        self._closed, self._task = False, None
        self._generation = 0
        self._selection = 0
        self._reviewed, self._after = None, None
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
        self.more_button = QPushButton("加载更多")
        self.more_button.clicked.connect(self.load_more)
        self.more_button.setEnabled(False)
        actions.addWidget(self.more_button)
        actions.addWidget(self.accept_button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def reload(self):
        self._generation += 1
        self.list.clear()
        self.diff.clear()
        self._reviewed, self._after = None, None
        self.accept_button.setEnabled(False)
        self.load_more()

    def _read(self, operation, callback, obsolete):
        if self.scope.knowledge is None:
            self.window.io.submit(operation, callback, kb=self.kb, obsolete=obsolete)
            return
        from ultrakb.storage.coordination import storage_stage

        def read():
            with storage_stage(
                self.kb, self.scope.knowledge.identity, read_only=True, cancelled=obsolete
            ):
                return operation()

        self.window.io.submit(read, callback, obsolete=obsolete)

    def load_more(self):
        generation = self._generation
        after = self._after
        self.more_button.setEnabled(False)

        def loaded(page, error):
            if self._closed or generation != self._generation:
                return
            if error:
                self.status.setText(f"无法读取建议（{type(error).__name__}）")
                return
            for view in page.proposals:
                item = QListWidgetItem(f"{view.proposal_id} · {view.view_id} · {view.status}")
                item.setData(Qt.ItemDataRole.UserRole, view)
                self.list.addItem(item)
            self._after = page.next_after
            self.more_button.setEnabled(self._after is not None)
            self.status.setText(f"已载入 {self.list.count()} 项建议。选择后读取差异，再接受。")

        self._read(
            lambda: list_proposal_page(self.kb, scope=self.scope, after=after),
            loaded,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def selected(self, item, previous=None):
        self._selection += 1
        selected = self._selection
        self._reviewed = None
        self.accept_button.setEnabled(False)
        self.diff.clear()
        if item is None:
            return
        summary = item.data(Qt.ItemDataRole.UserRole)

        def loaded(view, error):
            if self._closed or selected != self._selection:
                return
            if error:
                self.status.setText(f"无法读取差异（{type(error).__name__}）")
                return
            self._reviewed = view
            self.diff.setPlainText(view.diff)
            self.accept_button.setEnabled(
                view.status == "awaiting_confirmation" and self._task is None
            )

        self._read(
            lambda: read_proposal(self.kb, summary.proposal_id, scope=self.scope),
            loaded,
            obsolete=lambda: self._closed or selected != self._selection,
        )

    def accept_selected(self):
        item = self.list.currentItem()
        if item is None or self._task or self._reviewed is None:
            return
        view = self._reviewed
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
