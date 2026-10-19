"""Read and operate one worksheet while showing the workbook's independent outcomes."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ultrakb.runtime.records import TERMINAL


class WorksheetsDialog(QDialog):
    def __init__(self, window, kb, source, *, read_scope=None):
        super().__init__(window)
        self.window, self.kb, self.source = window, kb, source
        self.scope = getattr(window, "scope", None)
        self.read_scope = read_scope
        if self.read_scope is None and source.get("publication_id"):
            from ultrakb.application.views import published_scope

            self.read_scope = published_scope(kb, publication_id=source["publication_id"])
        self._closed, self._busy, self._task = False, False, None
        self.setWindowTitle(f"工作表 · {source['name']}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 620)
        layout = QVBoxLayout(self)
        self.status = QLabel("一个工作簿来源；各工作表独立发布。重试只处理所选未完成项。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setAccessibleName("工作表处理单元")
        self.table.setHorizontalHeaderLabels(
            ["工作表", "状态", "分类 / 编译", "目标来源修订", "实际来源修订", "知识修订"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.buttons = []
        for label, callback in (
            ("刷新工作表", self.reload),
            ("阅读所选工作表", self.read),
            ("重试所选工作表", self.retry),
            ("重编译所选工作表", self.recompile),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.render(source.get("available_units", []))

    def render(self, units):
        self.table.setRowCount(len(units))
        for row, unit in enumerate(units):
            values = (
                unit["name"],
                {"empty": "已确认空表", "retired": "已删除工作表"}.get(
                    unit["status"],
                    {
                        "objects_only": "只有对象，未提取单元格",
                        "parse_failed": "工作表解析失败",
                    }.get(unit.get("content_state"), unit["status"]),
                ),
                f"{unit.get('length_class') or '未知'} / {unit.get('execution_mode') or '未记录'}",
                unit.get("target_source_revision_id"),
                unit.get("successful_source_revision_id"),
                unit.get("knowledge_revision_id"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or "—"))
                item.setData(Qt.ItemDataRole.UserRole, unit)
                item.setToolTip(unit.get("message") or str(value or ""))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        if units:
            self.table.selectRow(0)

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None

    def operate(self, function, callback, *, snapshot=False):
        if self._busy or self._task:
            return
        self._busy = True
        for button in self.buttons:
            button.setEnabled(False)

        def loaded(value, error):
            self._busy = False
            for button in self.buttons:
                button.setEnabled(True)
            if error:
                self.status.setText(str(error))
            elif value is not None:
                callback(value)

        self.window.io.submit(
            function, loaded, kb=self.kb, snapshot=snapshot, obsolete=lambda: self._closed
        )

    def reload(self):
        from ultrakb.documents import read_document_source

        self.operate(
            lambda: read_document_source(
                self.kb, self.source["source_id"], scope=self.read_scope or self.scope
            ),
            lambda source: self.render(source["available_units"]),
            snapshot=self.read_scope is not None,
        )

    def read(self):
        from ultrakb.desktop.source_reader import show_source
        from ultrakb.documents import read_document_view

        unit = self.selected()
        if unit:
            self.operate(
                lambda: read_document_view(
                    self.kb,
                    self.source["source_id"],
                    unit_id=unit["unit_id"],
                    unit_revision_id=unit["target_revision_id"]
                    if not unit.get("successful_revision_id")
                    else None,
                    scope=self.read_scope or self.scope,
                ),
                lambda source: show_source(self.window, self.kb, source),
                snapshot=self.read_scope is not None,
            )

    def retry(self):
        from ultrakb.runtime.requests import RetryWorksheet

        unit = self.selected()
        if unit and not self._busy and not self._task:
            if unit["status"] == "completed":
                self.status.setText("所选工作表已完成；需要再次生成知识时使用重编译。")
                return
            from ultrakb.desktop.model_actions import submit_panel

            submit_panel(
                self,
                [
                    RetryWorksheet(
                        self.source["source_id"], unit["unit_id"], view_id=self.source["view_id"]
                    )
                ],
                description="重试所选工作表",
            )

    def recompile(self):
        from ultrakb.application.recompilation import select_recompilation
        from ultrakb.runtime.requests import RecompileDocument

        unit = self.selected()
        if not unit:
            return

        def ready(selection):
            if selection.status != "ready":
                self.status.setText("所选工作表已变化，请刷新。")
                return
            from ultrakb.desktop.model_actions import submit_panel

            submit_panel(
                self,
                [
                    RecompileDocument(
                        self.source["source_id"],
                        selection.version,
                        unit_id=unit["unit_id"],
                        view_id=self.source["view_id"],
                    )
                ],
                description="重新生成所选工作表知识",
            )

        self.operate(
            lambda: select_recompilation(
                self.kb,
                self.source["source_id"],
                unit_id=unit["unit_id"],
                confirmation=True,
                scope=self.scope,
            ),
            ready,
        )

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state in TERMINAL:
            self._task = None
            self.status.setText(
                "\n".join([task.state, *(result.error or "" for result in task.results)])
            )
            self.reload()

    def done(self, result):
        self._closed = True
        super().done(result)
