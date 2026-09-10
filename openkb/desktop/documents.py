"""Native document inventory and version-bound removal confirmation."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openkb.application.knowledge_bases import get_kb_list
from openkb.application.recompilation import select_recompilation
from openkb.application.removal import preview_removal
from openkb.desktop.flow_layout import FlowLayout
from openkb.desktop.panels import ManagementPanel
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import RecompileDocument, RemoveDocument


class DocumentsDialog(ManagementPanel):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self._generation = 0
        self._confirmed = None
        self._task = None
        self.setWindowTitle(f"资料管理 · {kb.name}")
        self.resize(880, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        from openkb.desktop.location import LocationLabel

        location = LocationLabel(str(kb))
        location.setObjectName("muted")
        layout.addWidget(location)
        inventory = QHBoxLayout()
        self.selection_status = QLabel("正在读取资料…")
        inventory.addWidget(self.selection_status, 1)
        self.refresh_button = QPushButton("刷新资料")
        self.refresh_button.clicked.connect(self.reload)
        inventory.addWidget(self.refresh_button)
        layout.addLayout(inventory)
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("documentTable")
        self.table.setAccessibleName("资料列表")
        self.table.setHorizontalHeaderLabels(["资料", "类型", "原文", "知识编译"])
        self._documents = {}
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumSectionSize(88)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(220)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemActivated.connect(lambda _item: self.review_source())
        layout.addWidget(self.table, 1)
        actions = FlowLayout()
        self.review_button = QPushButton("查看原文与处理结果")
        self.review_button.setObjectName("primaryAction")
        self.review_button.setToolTip("查看所选资料的原文、证据与待接受的知识变更")
        self.review_button.clicked.connect(self.review_source)
        self.recompile_selected = QPushButton("重编译所选资料")
        self.recompile_all = QPushButton("重编译全部资料")
        self.recompile_selected.clicked.connect(lambda: self.recompile(all_docs=False))
        self.recompile_all.clicked.connect(lambda: self.recompile(all_docs=True))
        self.removal_toggle = QPushButton("删除资料…")
        self.removal_toggle.setCheckable(True)
        for button in (
            self.review_button,
            self.recompile_selected,
            self.recompile_all,
            self.removal_toggle,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.removal_panel = QFrame()
        self.removal_panel.setObjectName("documentRemoval")
        removal = QVBoxLayout(self.removal_panel)
        hint = QLabel("先查看所选资料的删除计划，再确认删除。")
        hint.setWordWrap(True)
        removal.addWidget(hint)
        options = FlowLayout()
        self.keep_raw = QCheckBox("保留原文")
        self.keep_empty = QCheckBox("保留失去全部来源的概念 / 实体页面")
        for checkbox in (self.keep_raw, self.keep_empty):
            checkbox.toggled.connect(self.invalidate)
            options.addWidget(checkbox)
        removal.addLayout(options)
        deletion = FlowLayout()
        self.preview_button = QPushButton("查看删除计划")
        self.confirm_button = QPushButton("确认删除")
        self.confirm_button.setObjectName("dangerAction")
        self.confirm_button.setEnabled(False)
        for button, callback in (
            (self.preview_button, self.preview),
            (self.confirm_button, self.confirm),
        ):
            button.clicked.connect(callback)
            deletion.addWidget(button)
        removal.addLayout(deletion)
        self.removal_panel.hide()
        self.removal_toggle.toggled.connect(self.removal_panel.setVisible)
        self.removal_toggle.toggled.connect(self.invalidate)
        layout.addWidget(self.removal_panel)
        self.status = QLabel("选择一份资料查看原文；按住 Ctrl 或 Shift 可选择多份资料。")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        self.details.hide()
        layout.addWidget(self.details)
        self.table.itemSelectionChanged.connect(self.invalidate)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def invalidate(self):
        self._generation += 1
        if self._confirmed is not None:
            self.details.clear()
            self.details.hide()
            self.status.setText("删除计划已失效，请重新查看。")
        self._confirmed = None
        self.confirm_button.setEnabled(False)
        self.update_selection()

    def update_selection(self):
        count = len(self.table.selectionModel().selectedRows())
        total = self.table.rowCount()
        self.selection_status.setText(
            f"{total} 份资料" + (f" · 已选择 {count} 份" if count else "")
        )
        available = self._task is None
        self.review_button.setEnabled(count == 1)
        self.preview_button.setEnabled(count == 1 and available)
        self.removal_toggle.setEnabled(
            self.removal_toggle.isChecked() or (count == 1 and available)
        )
        self.recompile_selected.setEnabled(count > 0 and available)
        self.recompile_all.setEnabled(total > 0 and available)

    def reload(self, *, preserve_result=False):
        self.invalidate()

        def loaded(value, error):
            if self._closed:
                return
            if error:
                self.status.setText(f"读取资料失败（{type(error).__name__}）")
                return
            if not preserve_result:
                self.status.setText(
                    "选择一份资料查看原文；按住 Ctrl 或 Shift 可选择多份资料。"
                    if value["documents"]
                    else "暂无资料。从上方导入文件、目录或网址。"
                )
            self.table.setRowCount(len(value["documents"]))
            self._documents = {doc["hash"]: doc for doc in value["documents"]}
            for row, doc in enumerate(value["documents"]):
                item = QTableWidgetItem(doc["name"])
                item.setToolTip(doc["name"])
                item.setData(Qt.ItemDataRole.UserRole, doc["hash"])
                self.table.setItem(row, 0, item)
                kind = doc.get("display_type", doc["type"])
                kind = {"short": "短文档", "long_pdf": "长篇 PDF"}.get(kind, kind)
                self.table.setItem(row, 1, QTableWidgetItem(kind))
                self.table.setItem(
                    row,
                    2,
                    QTableWidgetItem(
                        "已保存" if doc.get("source_intake") == "saved" else "旧版资料"
                    ),
                )
                labels = {
                    "completed": "完成",
                    "unfinished": "未完成",
                    "failed": "失败",
                    "stopped": "已停止",
                }
                self.table.setItem(
                    row, 3, QTableWidgetItem(labels.get(doc.get("knowledge_compilation"), "待处理"))
                )
            self.update_selection()

        self.window.io.submit(
            lambda: get_kb_list(self.kb), loaded, kb=self.kb, obsolete=lambda: self._closed
        )

    def preview(self):
        row = self.table.currentRow()
        if row < 0 or self._task or len(self.table.selectionModel().selectedRows()) != 1:
            self.status.setText("请只选择一份资料查看删除计划。")
            return
        identifier = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        keep_raw, keep_empty = self.keep_raw.isChecked(), self.keep_empty.isChecked()
        self.invalidate()
        generation = self._generation
        self.status.setText("正在读取最新删除计划…")

        def loaded(value, error):
            if self._closed or generation != self._generation:
                return
            if error:
                self.status.setText(f"计划读取失败（{type(error).__name__}）")
                return
            if value.status != "ready":
                self.status.setText("资料已变化，请刷新资料列表。")
                return
            self._confirmed = RemoveDocument(identifier, value.version, keep_raw, keep_empty)
            self.details.setPlainText("\n".join(f"{a.tag}  {a.target}" for a in value.plan.actions))
            self.details.show()
            self.status.setText("确认后执行以上清理。若知识库已变化，将要求重新查看并确认计划。")
            self.confirm_button.setEnabled(True)

        self.window.io.submit(
            lambda: preview_removal(self.kb, identifier, keep_raw=keep_raw, keep_empty=keep_empty),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def review_source(self):
        if len(self.table.selectionModel().selectedRows()) != 1:
            self.status.setText("请选择一份资料查看原文与处理结果。")
            return
        identifier = self.table.item(self.table.currentRow(), 0).data(Qt.ItemDataRole.UserRole)
        source_id = self._documents.get(identifier, {}).get("source_id")
        if not source_id:
            self.status.setText("这是旧版资料。重编译会保留已有来源，生成变更后可在这里审阅。")
            return
        from openkb.desktop.source_review import SourceReview

        SourceReview(self.window, self.kb, source_id).exec()
        self.reload()

    def confirm(self):
        if self._confirmed is None or self._task:
            return
        self._task = self.window.manager.submit(self.kb, [self._confirmed])
        self.invalidate()
        self.status.setText("删除任务已提交，可在主窗口查看状态或安全停止。")

    def recompile(self, *, all_docs):
        if self._task:
            return
        selected = {
            self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in self.table.selectionModel().selectedRows()
        }
        if not all_docs and not selected:
            self.status.setText("请先选择要重编译的资料。")
            return
        self.invalidate()
        generation = self._generation

        def loaded(selection, error):
            if self._closed or generation != self._generation or self._task:
                return
            if error:
                self.status.setText(f"无法读取重编译资料（{type(error).__name__}）")
                return
            targets = [t for t in selection.targets if all_docs or t.file_hash in selected]
            if not targets:
                self.status.setText("没有可重编译的资料，请刷新列表。")
                return
            question = QMessageBox(self)
            question.setWindowTitle("确认重编译")
            question.setText(f"重编译 {len(targets)} 份资料？")
            question.setInformativeText(
                "将使用已保存的原文重新生成摘要、概念和实体页面。"
                "手工编辑的页面会保留，待你审阅并接受差异后更新。停止任务会保留已完成项。"
            )
            question.setDetailedText("\n".join(t.doc_name for t in targets))
            question.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            question.setDefaultButton(QMessageBox.StandardButton.No)
            if question.exec() != QMessageBox.StandardButton.Yes:
                return
            self._task = self.window.manager.submit(
                self.kb, [RecompileDocument(t.file_hash, selection.version) for t in targets]
            )
            self.update_selection()
            self.status.setText("重编译任务已提交，可在主窗口查看逐项结果或安全停止。")

        self.window.io.submit(
            lambda: select_recompilation(self.kb, all_docs=True, confirmation=True),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def poll(self):
        if self._task is None:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        lines = []
        for result in task.results:
            if result.error:
                lines.append(result.error)
            lines.extend(f"质量提示：{note}" for note in result.quality)
            lines.extend(result.changes)
            lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
            lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        if task.error and not lines:
            lines.append(task.error)
        self.details.setPlainText("\n".join(lines))
        self.details.setVisible(bool(lines))
        self.status.setText(
            "任务已执行，含质量提示或未完成阶段，请查看结果。"
            if any(result.quality or result.unfinished for result in task.results)
            else "任务完成。"
            if task.state == "completed"
            else "任务未全部完成。请查看逐项结果；确认最新资料后可手动重试。"
        )
        self.reload(preserve_result=True)

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
