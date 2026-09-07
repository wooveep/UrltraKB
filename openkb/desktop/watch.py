"""Native controls for independent raw subscriptions across open knowledge bases."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openkb.runtime.watch import NativeWatchRegistry

_STATES = {
    "scanning": "启动补查",
    "watching": "监听中",
    "waiting": "等待输入 / 知识库",
    "blocked": "需要检查",
    "stopping": "正在停止",
    "stopped": "已停止",
    "failed": "已结束",
}


class WatchDialog(QDialog):
    def __init__(self, registry: NativeWatchRegistry, root: Path | None, parent=None):
        super().__init__(parent)
        self.registry, self.root = registry, root
        self.setWindowTitle("目录监听 · 所有知识库")
        self.resize(1080, 450)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "手动启用后补查 raw 中已有资料，并导入新增或修改后稳定的文件。\n"
            "停止监听后，已提交任务继续，可在任务列表单独停止。重启程序不会自动恢复监听。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["知识库", "状态", "启动补查 / 提交", "待处理 / 运行", "提示"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.start_button = QPushButton(f"启用当前库：{root.name}" if root else "请先打开知识库")
        self.start_button.setEnabled(root is not None)
        self.start_button.clicked.connect(self.start)
        actions.addWidget(self.start_button)
        self.stop_button = QPushButton("停止所选监听")
        self.stop_button.clicked.connect(self.stop)
        actions.addWidget(self.stop_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(150)
        self.refresh()

    def start(self):
        if self.root is None:
            return
        try:
            self.registry.start(self.root)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "监听未启用", str(exc))
        self.refresh()

    def stop(self):
        row = self.table.currentRow()
        if row >= 0:
            root = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for watch in self.registry.watches():
                if str(watch.root) == root:
                    watch.stop()
                    break
        self.refresh()

    def refresh(self):
        watches = self.registry.watches()
        self.table.setRowCount(len(watches))
        for row, watch in enumerate(watches):
            view = watch.view()
            values = (
                Path(view.kb_dir).name,
                _STATES.get(view.state, view.state),
                f"扫描发现 {view.startup_found} · 提交 {view.submitted}",
                f"候选 {view.pending} · 已提交未结束 {view.active}",
                view.error
                or (
                    f"候选数量达上限；已补查 {view.overflow_scans} 轮"
                    if view.overflow_scans
                    else ""
                ),
            )
            for column, text in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(text)
                item.setToolTip(view.kb_dir if column == 0 else text)
                item.setData(Qt.ItemDataRole.UserRole, view.kb_dir)
        self.table.resizeColumnsToContents()

    def done(self, result):
        self.timer.stop()
        super().done(result)
