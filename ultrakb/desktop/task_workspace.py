"""Task browsing and explicit checkbox scope, independent of row focus and polling."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ultrakb.desktop.flow_layout import FlowLayout
from ultrakb.desktop.form_controls import FocusComboBox
from ultrakb.desktop.task_status import (
    OPERATIONS,
    STATES,
    current_source,
    needs_attention,
    task_counts,
    task_progress,
)
from ultrakb.runtime.records import TERMINAL


class TaskWorkspace(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.checked = set()
        self.tasks = {}
        self.visible = ()
        self._last = None
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        toolbar = FlowLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索任务、资料或知识库")
        self.search.setAccessibleName("搜索任务")
        self.search.setMinimumWidth(180)
        self.state_filter = FocusComboBox()
        self.state_filter.addItems(["全部", "进行中", "需关注", "已结束"])
        self.state_filter.setAccessibleName("任务状态筛选")
        self.scope_filter = FocusComboBox()
        self.scope_filter.addItems(["所有知识库", "当前知识库"])
        self.scope_filter.setAccessibleName("任务知识库范围")
        select = QPushButton("全选当前结果")
        select.clicked.connect(self.select_visible)
        for widget in (self.search, self.state_filter, self.scope_filter, select):
            toolbar.addWidget(widget)
        body.addLayout(toolbar)
        self.table = window.task_table = QTableWidget(0, 6)
        self.table.setAccessibleName("知识库任务")
        self.table.setHorizontalHeaderLabels(
            ["知识库", "任务 / 资料", "状态", "进度", "当前阶段", "操作"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column, width in ((1, 100), (2, 80), (3, 154), (4, 210), (5, 176)):
            self.table.setColumnWidth(column, width)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(window._select_task)
        self.table.itemChanged.connect(self.item_changed)
        self.table.itemDoubleClicked.connect(lambda item: self.details(self.identity(item.row())))
        body.addWidget(self.table, 1)
        self.bulk = QWidget()
        selected = QVBoxLayout(self.bulk)
        selected.setContentsMargins(0, 0, 0, 0)
        self.count = QLabel()
        self.count.setWordWrap(True)
        selected.addWidget(self.count)
        actions = FlowLayout()
        self.stop = QPushButton("安全停止所选任务")
        self.retry = QPushButton("重试所选未完成任务…")
        self.clear = QPushButton("清理所选任务摘要…")
        cancel = QPushButton("取消选择")
        self.stop.clicked.connect(self.stop_checked)
        self.retry.clicked.connect(lambda: self.retry_ids(tuple(self.checked)))
        self.clear.clicked.connect(lambda: self.clear_ids(tuple(self.checked)))
        cancel.clicked.connect(self.clear_selection)
        for widget in (self.stop, self.retry, self.clear, cancel):
            actions.addWidget(widget)
        selected.addLayout(actions)
        body.addWidget(self.bulk)
        self.bulk.hide()
        self.status = QLabel("暂无任务。导入、提问等操作会在这里显示进度。")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        self.owner = QLabel("选择任务以查看其所属知识库与保留产物。")
        self.owner.setWordWrap(True)
        self.owner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.owner)
        self.artifacts = window.artifacts = QListWidget()
        self.artifacts.setMaximumHeight(120)
        self.artifacts.hide()
        body.addWidget(self.artifacts)
        for signal in (
            self.search.textChanged,
            self.state_filter.currentIndexChanged,
            self.scope_filter.currentIndexChanged,
        ):
            signal.connect(self.filter_changed)
        self.select_all = QShortcut(QKeySequence.StandardKey.SelectAll, self.table)
        self.select_all.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.select_all.activated.connect(self.select_visible)

    def identity(self, row):
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def filter_changed(self):
        had_selection = bool(self.checked)
        self.checked.clear()
        self.refresh(tuple(self.tasks.values()), force=True)
        if had_selection:
            self.status.setText("筛选条件已变化，已清空勾选；请重新确认批量范围。")

    def refresh(self, tasks, *, force=False):
        query, state = self.search.text().strip().casefold(), self.state_filter.currentText()
        signature = tasks, str(self.window.kb), query, state, self.scope_filter.currentIndex()
        if not force and signature == self._last:
            return
        self._last = signature
        self.tasks = {task.id: task for task in tasks}
        self.checked.intersection_update(self.tasks)
        visible = [
            task
            for task in tasks
            if (
                (not self.scope_filter.currentIndex() or task.kb_dir == str(self.window.kb))
                and (state != "进行中" or task.state not in TERMINAL)
                and (state != "已结束" or task.state in TERMINAL)
                and (state != "需关注" or needs_attention(task))
                and query
                in (
                    " ".join(
                        (
                            task.id,
                            task.kb_dir,
                            task.operation,
                            OPERATIONS.get(task.operation, ""),
                            current_source(task),
                        )
                    )
                ).casefold()
            )
        ]
        old = [self.identity(row) for row in range(self.table.rowCount())]
        new = {task.id for task in visible}
        rebuild = set(old) != new
        focused = self.identity(self.table.currentRow())
        blocked = self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        if rebuild:
            self.table.setRowCount(0)
            self.table.setRowCount(len(visible))
        rows = {identity: index for index, identity in enumerate(old)}
        for index, task in enumerate(visible):
            row = index if rebuild else rows[task.id]
            source = current_source(task)
            values = [
                Path(task.kb_dir).name,
                OPERATIONS.get(task.operation, task.operation) + (f"\n{source}" if source else ""),
                STATES[task.state],
                task_counts(task) + "\n" + task_progress(task),
                task_progress(task),
            ]
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(value)
                item.setData(Qt.ItemDataRole.UserRole, task.id)
                item.setToolTip(task.kb_dir + "\n" + task.id if column == 0 else value)
                if column == 0:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if task.id in self.checked
                        else Qt.CheckState.Unchecked
                    )
            controls = self.table.cellWidget(row, 5)
            if controls is None:
                controls = self.row_controls(task.id)
                self.table.setCellWidget(row, 5, controls)
            controls.findChild(QPushButton, "stopTask").setEnabled(
                task.state not in TERMINAL and not task.stop_requested and not self.window._quitting
            )
            controls.ensurePolished()
            self.table.setRowHeight(row, max(72, controls.minimumSizeHint().height() + 16))
        self.table.setSortingEnabled(True)
        self.table.blockSignals(blocked)
        self.visible = tuple(task.id for task in visible)
        if rebuild and focused in new:
            self.table.setCurrentItem(
                next(
                    self.table.item(row, 0)
                    for row in range(self.table.rowCount())
                    if self.identity(row) == focused
                )
            )
        self.update_bulk()
        self.status.setText(
            f"显示 {len(visible)} / {len(tasks)} 个任务。勾选后批量操作，行操作仅作用于该任务。"
            if visible
            else "没有符合筛选条件的任务。"
            if tasks
            else "暂无任务。导入、提问等操作会在这里显示进度。"
        )

    def row_controls(self, identity):
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)
        details = QPushButton("详情")
        details.setAccessibleName(f"查看任务 {identity} 详情")
        details.clicked.connect(lambda: self.details(identity))
        stop = QPushButton("停止")
        stop.setObjectName("stopTask")
        stop.setAccessibleName(f"停止任务 {identity}")
        stop.clicked.connect(lambda: self.stop_ids((identity,)))
        more = QToolButton()
        more.setText("更多")
        more.setAccessibleName(f"更多任务操作 {identity}")
        more.clicked.connect(lambda: self.row_menu(identity, more))
        for widget in (details, stop, more):
            row.addWidget(widget)
        return controls

    def row_menu(self, identity, button):
        menu = QMenu(button)
        heading = menu.addAction(f"仅此任务：{identity[:8]}")
        heading.setEnabled(False)
        menu.addAction("核对并重试未完成项…", lambda: self.retry_ids((identity,)))
        menu.addAction("清理任务摘要…", lambda: self.clear_ids((identity,)))
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        self._row_menu = menu

    def item_changed(self, item):
        if item.column() != 0:
            return
        identity = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(identity)
        else:
            self.checked.discard(identity)
        self.update_bulk()

    def select_visible(self):
        self.checked.update(self.visible)
        self.refresh(tuple(self.tasks.values()), force=True)

    def clear_selection(self):
        self.checked.clear()
        self.refresh(tuple(self.tasks.values()), force=True)

    def update_bulk(self):
        tasks = [self.tasks[identity] for identity in self.checked if identity in self.tasks]
        active = sum(t.state not in TERMINAL and not t.stop_requested for t in tasks)
        retry = sum(needs_attention(t) for t in tasks)
        closed = sum(t.state in TERMINAL for t in tasks)
        self.bulk.setVisible(bool(tasks))
        self.count.setText(
            f"已选 {len(tasks)} 项 · {active} 项可停止 · {retry} 项可核对重试 · "
            f"{closed} 项已结束，可核对清理。其他项将说明原因。"
        )
        self.stop.setEnabled(bool(active))
        self.retry.setEnabled(bool(retry) and not self.window._quitting)
        self.clear.setEnabled(bool(closed) and not self.window._quitting)

    def stop_checked(self):
        self.stop_ids(tuple(self.checked))

    def stop_ids(self, identities):
        stopped, skipped = 0, 0
        for identity in sorted(identities):
            try:
                task = self.window.manager.get(identity)
                if task.state in TERMINAL or task.stop_requested:
                    skipped += 1
                    continue
                self.window.manager.stop(identity)
                stopped += 1
            except KeyError:
                skipped += 1
        self.status.setText(
            f"已请求安全停止 {stopped} 项；跳过 {skipped} 项已结束、正在停止或已清理任务。"
            "已完成结果保留。"
        )

    def details(self, identity):
        from ultrakb.desktop.task_details import show_task_details

        try:
            show_task_details(self.window.manager.get(identity), self.window)
        except KeyError:
            self.status.setText("任务摘要已清理。")

    def retry_ids(self, identities):
        from ultrakb.desktop.task_actions import RetryDialog

        if identities:
            RetryDialog(self.window, identities).exec()

    def clear_ids(self, identities):
        from ultrakb.desktop.task_actions import clear_selected_history

        clear_selected_history(self.window, identities)

    def resizeEvent(self, event):
        self.table.setColumnHidden(4, self.width() < 1000)
        super().resizeEvent(event)
