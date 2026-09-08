"""Native local KB inventory, statistics, and confirmed directory removal."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openkb.application.catalog import knowledge_base_overview, knowledge_bases
from openkb.desktop.io import _defer_wait
from openkb.kb_admin import delete_kb
from openkb.lifecycle import deletion_binding
from openkb.locks import LockCancelled
from openkb.runtime.records import TERMINAL


class KnowledgeBasesDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._closed = self._deleting = self._cancel = self._close_requested = False
        self._selection = 0
        self.setWindowTitle("知识库管理")
        self.resize(920, 600)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["知识库", "完整目录"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.inspect)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.buttons = []
        for label, callback in (
            ("刷新列表", self.reload),
            ("打开所选知识库", self.open),
            ("删除所选知识库…", self.remove),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.buttons.append(button)
        self.cancel_button = QPushButton("撤回等待中的删除")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status = QLabel("正在读取知识库列表…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.reload()

    def selected(self) -> Path | None:
        row = self.table.currentRow()
        return Path(self.table.item(row, 1).text()) if row >= 0 else None

    def reload(self):
        if self._deleting:
            return
        self._selection += 1
        generation = self._selection

        def loaded(values, error):
            if error:
                self.status.setText(f"读取列表失败（{type(error).__name__}）")
                return
            selected = self.selected() or self.window.kb
            self.table.blockSignals(True)
            self.table.setRowCount(len(values))
            for row, (label, root) in enumerate(values):
                self.table.setItem(row, 0, QTableWidgetItem(label))
                self.table.setItem(row, 1, QTableWidgetItem(str(root)))
                if root == selected:
                    self.table.selectRow(row)
            self.table.blockSignals(False)
            if self.status.text().startswith("正在读取知识库列表"):
                self.status.setText(f"已读取 {len(values)} 个知识库。")
            self.window._recent_loaded(values, None)
            self.inspect()

        self.window.io.submit(
            knowledge_bases,
            loaded,
            global_settings=True,
            obsolete=lambda: self._closed or generation != self._selection,
        )

    def inspect(self):
        if self._deleting:
            return
        self._selection += 1
        generation, root = self._selection, self.selected()
        if root is None:
            self.details.clear()
            return
        self.details.setPlainText(f"{root}\n\n正在读取已提交的统计；当前写入完成后显示。")

        def loaded(value, error):
            if error:
                self.details.setPlainText(
                    f"{root}\n\n无法读取统计（{type(error).__name__}）。"
                    "目录已缺失时可清理登记；部分删除失败后可明确重试删除。"
                )
                return
            labels = {
                "sources": "来源页面",
                "summaries": "摘要",
                "concepts": "概念",
                "entities": "实体",
                "reports": "报告",
                "explorations": "已保存回答",
            }
            lines = [
                str(root),
                f"已索引资料：{value['total_indexed']}",
                f"原始文件：{value['raw_count']}",
            ]
            lines.extend(
                f"{labels.get(name, name)}：{count}" for name, count in value["directories"].items()
            )
            lines.extend(
                [
                    f"最近编译：{value['last_compile'] or '暂无'}",
                    f"最近检查：{value['last_lint'] or '暂无'}",
                ]
            )
            self.details.setPlainText("\n".join(lines))

        self.window.io.submit(
            lambda: knowledge_base_overview(root),
            loaded,
            kb=root,
            obsolete=lambda: self._closed or generation != self._selection,
        )

    def open(self):
        root = self.selected()
        if root is not None and not self._deleting:
            self.window.open_knowledge_base(root)
            self.accept()

    def remove(self):
        root = self.selected()
        if root is None or self._deleting:
            return
        self._selection += 1
        selection = self._selection

        def prepared(binding, error):
            if error:
                self.status.setText(f"无法确认删除：{error}")
                return
            typed, accepted = QInputDialog.getText(
                self,
                "永久删除知识库",
                f"目录：{root}\n\n将永久删除原始资料、知识页面、对话和产物，"
                "并丢弃此库未保存的正文草稿。\n本程序会停止此库的监听和任务，"
                "等待其他入口的当前操作完成。此操作无法撤销。\n\n"
                f"输入目录名 {root.name} 确认：",
            )
            if accepted and typed == root.name:
                self._remove(root, binding)

        self.window.io.submit(
            lambda: deletion_binding(root),
            prepared,
            obsolete=lambda: self._closed or selection != self._selection,
        )

    def _remove(self, root, binding):
        self._deleting, self._cancel = True, False
        self.window._knowledge_base_deleting(root, True)
        self.table.setEnabled(False)
        for button in self.buttons:
            button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        watches = [watch for watch in self.window.watch_registry.watches() if watch.root == root]
        for watch in watches:
            watch.stop()

        def stop_tasks():
            for task in self.window.manager.tasks():
                if Path(task.kb_dir) == root and task.state not in TERMINAL:
                    self.window.manager.stop(task.id)

        stop_tasks()
        self.status.setText("正在停止此库的监听与任务，并等待目录可删除。")

        def remove():
            # Scans already holding a read lease may submit after the first
            # stop sweep. This exclusive lease closes that submission window.
            stop_tasks()
            if any(not watch.join(0) for watch in watches) or self.window.manager.has_work(root):
                _defer_wait()
            delete_kb(root, generation=binding, cancelled=lambda: self._cancel)

        def removed(_value, error):
            self._deleting = False
            self.window._knowledge_base_deleting(root, False)
            self.table.setEnabled(True)
            for button in self.buttons:
                button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            if error:
                self.status.setText(
                    "已撤回删除等待；监听与任务保持停止。"
                    if isinstance(error, LockCancelled)
                    else f"删除未完成：{error}。检查目录后可重新确认删除。"
                )
                if isinstance(error, LockCancelled) and self.window.kb == root:
                    self.window._refresh_current()
            else:
                self.window._removed_knowledge_base(root)
                self.status.setText(f"已删除 {root}。任务历史摘要仍保留。")
            self.reload()
            if self._close_requested:
                self.accept()

        self.window.io.submit(
            remove, removed, kb=root, deleting=True, cancelled=lambda: self._cancel
        )

    def cancel(self):
        self._cancel = True
        self.status.setText("正在撤回等待；若目录删除已经开始，将等待本次文件操作结束。")

    def done(self, result):
        if self._deleting:
            self._close_requested = True
            self.cancel()
            return
        self._closed = True
        super().done(result)
