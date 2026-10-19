"""Bounded candidate inspection and explicit disposal through actual application I/O."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ultrakb.application.storage_maintenance import (
    discard_storage_candidate,
    list_storage_candidates,
)
from ultrakb.desktop.io import LocalIO


class StorageCandidatesDialog(QDialog):
    def __init__(self, root, identity, parent=None):
        super().__init__(parent)
        self.root, self.identity = Path(root).resolve(), identity
        self.busy = self._mutating = self._closed = False
        self.cursor = self.last_result = None
        self.io = LocalIO(self)
        self.setWindowTitle("检查并放弃未发布候选")
        self.resize(800, 560)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取候选…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.more_button = QPushButton("加载更多")
        self.discard_button = QPushButton("放弃选中候选")
        self.refresh_button.clicked.connect(self.refresh)
        self.more_button.clicked.connect(self.load_more)
        self.discard_button.clicked.connect(self.discard_selected)
        for button in (self.refresh_button, self.more_button, self.discard_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.refresh()

    def _controls(self):
        self.refresh_button.setEnabled(not self.busy)
        self.more_button.setEnabled(not self.busy and self.cursor is not None)
        item = self.list.currentItem()
        self.discard_button.setEnabled(
            not self.busy
            and item is not None
            and item.data(Qt.ItemDataRole.UserRole).protected_reason is None
        )

    def _selected(self, *_):
        item = self.list.currentItem()
        if item is None:
            self.details.clear()
        else:
            candidate = item.data(Qt.ItemDataRole.UserRole)
            self.details.setPlainText(
                f"候选类型：{candidate.kind}\n视图：{candidate.view_id}\n"
                f"候选身份：{candidate.candidate_id}\n版本：{candidate.version}\n"
                f"公开基线：{candidate.expected_publication or '初始版本'}\n"
                f"保护原因：{candidate.protected_reason or '无'}\n\n"
                "放弃导入候选会同时放弃同组审阅副本。已发布历史、原件、核心用量和"
                "已接受的模型计算成果仍保留。随后可在存储维护中分批释放引用并清理孤立内容。"
            )
        self._controls()

    def _load(self, after=None):
        if self.busy or self._closed:
            return
        self.busy = True
        self._controls()

        def loaded(page, error):
            self.busy = False
            if error:
                self.status.setText(f"候选未更新：{error}")
            else:
                if after is None:
                    self.list.clear()
                for candidate in page.items:
                    item = QListWidgetItem(f"{candidate.kind} · {candidate.candidate_id[:12]}")
                    item.setData(Qt.ItemDataRole.UserRole, candidate)
                    self.list.addItem(item)
                self.cursor = page.next_cursor
                self.status.setText("候选已更新；执行时会重新校验版本、代次和保护状态。")
            self._controls()

        self.io.submit(
            lambda: list_storage_candidates(
                self.root, identity=self.identity, after=after, cancelled=lambda: self._closed
            ),
            loaded,
            obsolete=lambda: self._closed,
        )

    def refresh(self):
        self._load()

    def load_more(self):
        if self.cursor is not None:
            self._load(self.cursor)

    def discard_selected(self):
        item = self.list.currentItem()
        if self.busy or self._closed or item is None:
            return
        candidate = item.data(Qt.ItemDataRole.UserRole)
        if candidate.protected_reason is not None:
            return
        if (
            QMessageBox.question(
                self, "放弃未发布候选", "放弃选中候选及其同组导入审阅副本？这会结束它们的审阅。"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.busy = self._mutating = True
        self._controls()

        def discarded(result, error):
            self.busy = self._mutating = False
            if error:
                self.status.setText(f"候选未放弃：{error}")
            else:
                self.last_result = result
                states = {
                    "discarded": "候选已放弃，可在存储维护中分批释放引用及孤立内容。",
                    "busy": "存储正在使用中，候选仍保留。",
                    "blocked": "候选受保护，仍保留。",
                    "conflict": "候选版本已变化，请刷新后检查。",
                    "missing": "候选已不可用，请刷新。",
                }
                self.status.setText(states[result.status])
                if result.status == "discarded":
                    self.list.takeItem(self.list.row(item))
            self._controls()

        self.io.submit(
            lambda: discard_storage_candidate(
                self.root,
                candidate.candidate_id,
                version=candidate.version,
                kind=candidate.kind,
                identity=self.identity,
            ),
            discarded,
            obsolete=lambda: self._closed,
        )

    def done(self, result):
        if self.busy and self._mutating:
            self.status.setText("操作仍在访问存储，结束后可关闭。")
            return
        self._closed = True
        self.io.stop()
        super().done(result)
