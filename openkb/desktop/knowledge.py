"""Search, browse and act on knowledge, without exposing maintenance files."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from openkb.application.knowledge import KNOWLEDGE_LABELS
from openkb.desktop.form_controls import FocusComboBox
from openkb.desktop.shell import action


class KnowledgeDirectory(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.entries = ()
        self.setMinimumWidth(200)
        self.setMaximumWidth(340)
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 8, 0)
        heading = QLabel("我的知识")
        heading.setObjectName("sectionTitle")
        body.addWidget(heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题或简介…")
        self.search.setAccessibleName("搜索知识")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter)
        body.addWidget(self.search)
        self.order = FocusComboBox()
        self.order.addItems(["最近更新", "标题顺序"])
        self.order.setAccessibleName("知识排序")
        self.order.currentIndexChanged.connect(self.populate)
        body.addWidget(self.order)
        self.tree = QTreeWidget()
        self.tree.setObjectName("knowledgeDirectory")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setWordWrap(True)
        body.addWidget(self.tree, 1)
        self.summary = QLabel("导入资料后，知识会出现在这里。")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        body.addWidget(self.summary)
        body.addWidget(action("导入资料", lambda: window.shell.navigate("资料")))

    def set_entries(self, entries):
        self.entries = entries
        self.populate()

    def populate(self):
        tree = self.tree
        selected = self.window.page.path + ".md" if self.window.page else None
        blocked = tree.blockSignals(True)
        tree.clear()
        entries = (
            sorted(self.entries, key=lambda e: e.title.casefold())
            if self.order.currentIndex()
            else self.entries
        )
        for section, label in KNOWLEDGE_LABELS.items():
            values = [entry for entry in entries if entry.section == section]
            if not values:
                continue
            group = QTreeWidgetItem(tree, [label])
            group.setData(0, Qt.ItemDataRole.UserRole + 1, label)
            for entry in values:
                node = QTreeWidgetItem(group, [entry.title])
                node.setData(0, Qt.ItemDataRole.UserRole, entry.path)
                node.setData(
                    0,
                    Qt.ItemDataRole.UserRole + 1,
                    (entry.title + " " + entry.description).casefold(),
                )
                node.setToolTip(0, entry.description or entry.title)
                if entry.path == selected:
                    tree.setCurrentItem(node)
        tree.expandAll()
        tree.blockSignals(blocked)
        self.filter()

    def filter(self):
        query = self.search.text().strip().casefold()
        shown = 0
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            count = 0
            for row in range(group.childCount()):
                node = group.child(row)
                matches = query in node.data(0, Qt.ItemDataRole.UserRole + 1)
                node.setHidden(not matches)
                count += matches
            group.setHidden(not count)
            group.setText(0, f"{group.data(0, Qt.ItemDataRole.UserRole + 1)} · {count}")
            shown += count
        self.summary.setText(
            f"{shown} 篇知识 · 点击阅读，支持继续提问"
            if shown
            else "没有匹配的知识，试试其他关键词。"
            if self.entries
            else "还没有知识。先导入资料，完成整理后即可在这里阅读。"
        )

    def ask(self):
        page = self.window.page
        if not page or not self.window.kb:
            return
        self.window.conversations.new()
        self.window.shell.navigate("对话")
        self.window.question.setPlainText(f"请结合 [[{page.path}]]，帮我梳理要点和相关知识。")
        self.window.question.setFocus()
