"""Native document inventory and revision-bound removal confirmation."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from ultrakb.application.removal import preview_removal
from ultrakb.desktop.panels import ManagementPanel
from ultrakb.runtime.records import TERMINAL
from ultrakb.runtime.requests import RemoveDocument


class DocumentsDialog(ManagementPanel):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self.scope = getattr(window, "scope", None)
        self.read_scope = getattr(window, "read_scope", None)
        self.view_id = self.scope.view_id if self.scope else None
        self._closed = False
        self._generation = 0
        self._confirmed = None
        self._task = None
        self._batch_preparing = False
        self.setWindowTitle(f"资料管理 · {kb.name}")
        self.resize(880, 650)
        layout = QVBoxLayout(self)
        from ultrakb.desktop.location import LocationLabel

        layout.addWidget(LocationLabel(str(kb)))
        from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

        from ultrakb.desktop.document_rows import focused, row_command, selection_changed
        from ultrakb.desktop.form_controls import FocusComboBox

        self.rows = {}
        self._load_generation = 0
        self._inventory_loading = False
        self._reload_pending = False
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索资料名称")
        self.search.setAccessibleName("搜索资料")
        self.state_filter = FocusComboBox()
        self.state_filter.addItems(["全部", "可阅读", "未完成", "处理失败", "知识待更新"])
        self.state_filter.setAccessibleName("资料状态筛选")
        self.refresh_button = QPushButton("刷新资料")
        self.refresh_button.clicked.connect(self.reload)
        from ultrakb.desktop.document_selection import select_visible

        self.select_visible_button = QPushButton("全选当前结果")
        self.select_visible_button.clicked.connect(lambda: select_visible(self))
        for widget in (
            self.search,
            self.state_filter,
            self.refresh_button,
            self.select_visible_button,
        ):
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("已导入资料")
        self.table.setHorizontalHeaderLabels(["资料", "类型", "状态", "操作"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 150)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setMinimumSectionSize(48)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(lambda: selection_changed(self))
        self.table.itemDoubleClicked.connect(
            lambda _item: row_command(self, focused(self).identity, "read")
            if focused(self)
            else None
        )
        from ultrakb.desktop.document_rows import context_menu

        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda position: context_menu(self, position))
        layout.addWidget(self.table, 1)
        from ultrakb.desktop.document_selection import install

        install(self, layout)
        self.search.textChanged.connect(self.filter_changed)
        self.state_filter.currentIndexChanged.connect(self.filter_changed)
        self.status = QLabel("正在读取资料…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(160)
        self.details.hide()
        layout.addWidget(self.details)
        self.removal_controls = QWidget()
        options = QVBoxLayout(self.removal_controls)
        self.keep_raw = QCheckBox("保留原文（旧资料）")
        self.keep_empty = QCheckBox("保留失去全部来源的概念 / 实体页面（旧资料）")
        for checkbox in (self.keep_raw, self.keep_empty):
            checkbox.toggled.connect(self.invalidate)
            options.addWidget(checkbox)
        self.preview_button = QPushButton("重新查看移除计划")
        self.preview_button.clicked.connect(self.preview)
        self.confirm_button = QPushButton("确认移除")
        self.confirm_button.clicked.connect(self.confirm)
        self.confirm_button.setEnabled(False)
        options.addWidget(self.preview_button)
        options.addWidget(self.confirm_button)
        self.removal_controls.hide()
        layout.addWidget(self.removal_controls)
        # Retain command adapters used by existing integrations; row controls and
        # knowledge/task maintenance menus are their visible entry points.
        for name in (
            "read_button",
            "recompile_selected",
            "recompile_all",
            "reprocess_button",
            "retry_button",
            "proposals_button",
            "pending_button",
            "refresh_knowledge_button",
            "empty_source_button",
        ):
            button = QPushButton(self)
            button.hide()
            setattr(self, name, button)
        for name, callback in (
            ("read_button", self.read_source),
            ("recompile_selected", lambda: self.recompile(all_docs=False)),
            ("recompile_all", lambda: self.recompile(all_docs=True)),
            ("reprocess_button", self.reprocess),
            ("retry_button", self.retry_source),
            ("proposals_button", self.review_proposals),
            ("pending_button", self.pending_work),
            ("refresh_knowledge_button", self.refresh_knowledge),
            ("empty_source_button", self.confirm_empty),
        ):
            getattr(self, name).clicked.connect(callback)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def remove_checked(self):
        from ultrakb.desktop.batch_removal import execute

        execute(self, tuple(self.selection.checked))

    def retry_source(self):
        from ultrakb.desktop.document_batch_actions import retry

        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1 or self._task:
            self.status.setText("请选择一份资料重试未完成项。")
            return
        from ultrakb.desktop.document_rows import focused

        target = focused(self)
        if target is None or target.identity.kind != "source":
            self.status.setText("旧资料请先预览重新处理；不会自动转换旧正文。")
            return
        retry(self, (target.identity,))

    def reprocess(self):
        from ultrakb.desktop.reprocessing import ReprocessingDialog

        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1 or self._task:
            self.status.setText("请选择一份资料进行预览。")
            return
        source_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        ReprocessingDialog(self.window, self.kb, source_id, scope=self.scope).show()

    def pending_work(self):
        from ultrakb.desktop.pending import PendingDialog

        PendingDialog(self.window, self.kb).show()

    def refresh_knowledge(self):
        from ultrakb.desktop.refresh import RefreshDialog

        RefreshDialog(self.window, self.kb).show()

    def confirm_empty(self):
        from ultrakb.application.refresh import read_source_contribution_target
        from ultrakb.runtime.requests import ConfirmEmptySource

        selected = self.table.selectionModel().selectedRows()
        if len(selected) != 1 or self._task:
            self.status.setText("请选择一份资料确认当前贡献为空。")
            return
        identity = self.table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        generation = self._generation
        contribution_scope = self.read_scope or self.scope

        def loaded(source, error):
            if error:
                self.status.setText(str(error))
                return
            if (
                QMessageBox.question(
                    self,
                    "确认资料贡献为空",
                    f"确认 {source.name} 当前修订不再提供知识依据？"
                    "相关页面将待刷新，历史原文仍保留。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self._task = self.window.manager.submit(
                self.kb,
                [
                    ConfirmEmptySource(
                        source.source_id, source.target_generation, view_id=self.view_id
                    )
                ],
            )
            self.invalidate()

        self.window.io.submit(
            lambda: read_source_contribution_target(self.kb, identity, scope=contribution_scope),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def review_proposals(self):
        from ultrakb.desktop.proposals import ProposalsDialog

        dialog = ProposalsDialog(self.window, self.kb)
        dialog.show()

    def read_source(self):
        """Render the original pipeline's retained Markdown or per-page JSON text."""
        from ultrakb.documents import read_document_view

        selected = self.table.selectionModel().selectedRows()
        if len(selected) != 1:
            self.status.setText("请先选择一份资料阅读原文。")
            return
        identifier = self.table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        from ultrakb.desktop.document_navigation import load_document
        from ultrakb.desktop.document_rows import focused

        identity = focused(self).identity
        document = focused(self).document
        targets = document.get("units", [])
        retained = None
        if targets and not any(unit.get("successful_revision_id") for unit in targets):
            retained = targets[0]["target_revision_id"]
        read_scope = self.read_scope

        def loaded(value, error):
            if error or value is None:
                self.status.setText("原文读取失败，请刷新资料列表或查看任务结果。")
                return
            from ultrakb.desktop.source_reader import show_source

            if value.source.get("sheet") is not None:
                from ultrakb.desktop.worksheets import WorksheetsDialog

                WorksheetsDialog(self.window, self.kb, value.source, read_scope=value.scope).show()
            else:
                show_source(self.window, self.kb, value)

        load_document(
            self,
            identity,
            "阅读原文",
            lambda: read_document_view(
                self.kb, identifier, unit_revision_id=retained, scope=read_scope or self.scope
            ),
            loaded,
            snapshot=read_scope is not None,
        )

    def invalidate(self):
        self._generation += 1
        self._confirmed = None
        self.confirm_button.setEnabled(False)

    def filter_changed(self):
        from ultrakb.desktop.document_rows import render

        cleared = bool(self.selection.checked)
        self.selection.checked.clear()

        self.invalidate()
        self.removal_controls.hide()
        render(self)
        if cleared:
            self.status.setText("筛选已变化，已清空勾选。请重新选择当前结果中的资料。")

    def recompile_checked(self):
        from ultrakb.desktop.document_batch_actions import recompile

        recompile(self, tuple(self.selection.checked))

    def retry_checked(self):
        from ultrakb.desktop.document_batch_actions import retry

        retry(self, tuple(self.selection.checked))

    def reload(self, *, preserve_result=False, scope=None):
        from ultrakb.application.document_actions import document_inventory
        from ultrakb.desktop.document_rows import render

        if self._closed:
            return
        if scope is None and getattr(self.window, "read_scope", None) is not None:
            self.status.setText("正在刷新资料；当前列表和阅读内容保留。")
            self.window._refresh()
            return
        if self._inventory_loading:
            self._reload_pending = True
            return
        self._inventory_loading = True
        self.invalidate()
        self._load_generation += 1
        generation = self._load_generation
        self.status.setText(
            "正在刷新资料；当前列表和阅读内容保留。" if self.rows else "正在读取资料…"
        )
        read_scope = scope

        def loaded(value, error):
            self._inventory_loading = False
            if self._reload_pending:
                self._reload_pending = False
                QTimer.singleShot(0, self.reload)
            if self._closed or generation != self._load_generation:
                return
            if error:
                self.status.setText(
                    f"读取资料失败（{type(error).__name__}）。原列表保留；请重试刷新。"
                )
                return
            self.rows = {row.identity: row for row in value}
            self.read_scope = read_scope
            removed = self.selection.reconcile(self.rows)
            render(self)
            if removed:
                self.status.setText(
                    f"已移除 {len(removed)} 个消失资料的勾选；其余选择已按身份保留。"
                )
            self.recompile_all.setEnabled(bool(self.rows))
            self.read_button.setEnabled(bool(self.rows))

        self.window.io.submit(
            lambda: document_inventory(self.kb, scope=read_scope or self.scope),
            loaded,
            kb=self.kb,
            snapshot=read_scope is not None,
            obsolete=lambda: self._closed or generation != self._load_generation,
            on_wait=lambda: self.status.setText(
                "正在等待知识库完成当前写入后刷新资料；已显示的内容保留。"
            )
            if not getattr(self, "_navigation_notice", None)
            else None,
        )

    def preview(self):
        row = self.table.currentRow()
        if row < 0 or self._task or len(self.table.selectionModel().selectedRows()) != 1:
            self.status.setText("请只选择一份资料查看删除计划。")
            return
        identifier = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        from ultrakb.desktop.document_rows import focused

        target = focused(self)
        legacy = target is not None and target.identity.kind == "legacy"
        self.keep_raw.setVisible(legacy)
        self.keep_empty.setVisible(legacy)
        self.removal_controls.show()
        self.details.show()
        keep_raw = legacy and self.keep_raw.isChecked()
        keep_empty = legacy and self.keep_empty.isChecked()
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
            self._confirmed = RemoveDocument(
                identifier, value.version, keep_raw, keep_empty, view_id=self.view_id
            )
            self.details.setPlainText("\n".join(f"{a.tag}  {a.target}" for a in value.plan.actions))
            self.status.setText("确认后执行以上清理。若知识库已变化，将要求重新查看并确认计划。")
            self.confirm_button.setEnabled(True)

        self.window.io.submit(
            lambda: preview_removal(
                self.kb, identifier, keep_raw=keep_raw, keep_empty=keep_empty, scope=self.scope
            ),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def confirm(self):
        if self._confirmed is None or self._task:
            return
        self._task = self.window.manager.submit(self.kb, [self._confirmed])
        self.invalidate()
        self.status.setText("删除任务已提交，可在主窗口查看状态或安全停止。")

    def recompile(self, *, all_docs):
        from ultrakb.desktop.document_batch_actions import recompile
        from ultrakb.desktop.document_rows import focused

        target = focused(self)
        identities = tuple(self.rows) if all_docs else (target.identity,) if target else ()
        recompile(self, identities)

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
            for item in result.removal_items:
                status = {
                    "completed": "已移除",
                    "failed": "未完成",
                    "partial": "部分完成",
                    "not_run": "未执行",
                }[item.status]
                lines.append(f"{item.name} · {status} · {item.message or ''}")
            lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
            lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        if task.error and not lines:
            lines.append(task.error)
        self.details.setPlainText("\n".join(lines))
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
