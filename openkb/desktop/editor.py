"""Draft identity and explicit reconciliation against a freshly read page version."""

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from openkb.application.pages import Page


@dataclass(frozen=True)
class PageDraft:
    body: str
    version: str


class DraftDialog(QDialog):
    def __init__(self, page: Page, draft: PageDraft, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"核对最新版本 · {page.path}")
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("左侧为最新已保存内容。请在右侧整理正文，再确认保存。"))
        columns = QHBoxLayout()
        latest = QPlainTextEdit(page.body)
        latest.setReadOnly(True)
        latest.setPlaceholderText("当前磁盘版本")
        self.draft = QPlainTextEdit(draft.body)
        columns.addWidget(latest)
        columns.addWidget(self.draft)
        layout.addLayout(columns)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton("以右侧正文保存", QDialogButtonBox.ButtonRole.AcceptRole)
        assert save is not None
        save.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
