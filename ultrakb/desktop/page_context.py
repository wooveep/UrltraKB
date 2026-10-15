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
        page = context.page
        if page.validity != "current":
            status = (
                "待刷新：当前问答将排除这些旧依据"
                if page.validity == "needs_refresh"
                else "历史知识"
            )
            group = QTreeWidgetItem(self, [status])
            if page.knowledge_revision_id:
                QTreeWidgetItem(group, [f"知识修订：{page.knowledge_revision_id}"])
            for identity in page.source_revision_ids:
                QTreeWidgetItem(group, [f"实际来源修订：{identity}"])
            for reason in page.refresh_reasons:
                label = {"updated": "来源更新", "withdrawn": "来源撤回", "empty": "确认清空"}[
                    reason["kind"]
                ]
                QTreeWidgetItem(group, [label])
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
