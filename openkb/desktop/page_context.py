"""Native source/outbound/inbound navigation for the displayed committed page."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem


class PageContextView(QTreeWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.root = None
        self.setHeaderLabel("来源与链接 · 双击阅读")
        self.itemActivated.connect(self._open)

    def show_context(self, root, context):
        self.clear()
        self.root = root
        for title, references in (
            ("来源", context.sources),
            ("出链", context.outlinks),
            ("反向链接", context.backlinks),
        ):
            group = QTreeWidgetItem(self, [f"{title} · {len(references)}"])
            for reference in references:
                label = reference.label + ("（未找到页面）" if reference.path is None else "")
                item = QTreeWidgetItem(group, [label])
                item.setToolTip(0, reference.path or reference.label)
                item.setData(0, Qt.ItemDataRole.UserRole, reference)
        if context.problems:
            group = QTreeWidgetItem(self, ["反向链接扫描不完整"])
            for problem in context.problems:
                item = QTreeWidgetItem(group, [problem])
                item.setToolTip(0, problem)
        self.expandAll()

    def _open(self, item, _column):
        reference = item.data(0, Qt.ItemDataRole.UserRole)
        if self.window.kb == self.root and reference and reference.path:
            self.window.open_page(reference.path, reference.anchor)

    def invalidate(self):
        self.clear()
        QTreeWidgetItem(self, ["正文已保存，刷新页面可更新来源与链接。"])
