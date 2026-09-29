"""Explicit per-series choices for questions without a selected version."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openkb.application.query_views import list_family_defaults, select_default_view
from openkb.desktop.form_controls import FocusComboBox


class VersionDefaultsDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed = False
        self.setWindowTitle("问答默认版本")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "未指定版本的问题按各系列的默认值回答。没有默认值时分别回答各版；"
            "明确版本和比较问题使用所请求的范围。导入资料不会改变默认值。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["资料系列", "默认版本", ""])
        self.table.verticalHeader().setDefaultSectionSize(50)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.status = QLabel("正在读取系列…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        window.io.submit(
            lambda: list_family_defaults(kb), self.loaded, kb=kb, obsolete=lambda: self._closed
        )

    def loaded(self, families, error):
        if self._closed:
            return
        if error:
            self.status.setText(f"无法读取默认版本：{error}")
            return
        self.table.setRowCount(len(families))
        for row, family in enumerate(families):
            candidates = family["available_views"]
            product = candidates[0].product if candidates else "尚无已发布版本"
            label = QTableWidgetItem(f"{product} · {family['purpose']}")
            label.setToolTip(family["family_id"])
            self.table.setItem(row, 0, label)
            combo = FocusComboBox()
            combo.setAccessibleName(f"{family['purpose']} 默认版本")
            combo.addItem("无默认 · 分别回答", None)
            for view in candidates:
                combo.addItem(", ".join(view.applicable_versions), view.view_id)
            combo.setCurrentIndex(max(0, combo.findData(family["view_id"])))
            self.table.setCellWidget(row, 1, combo)
            button = QPushButton("保存选择")
            button.clicked.connect(
                lambda checked=False, f=family, c=combo, b=button: self.save(
                    f["family_id"], c.currentData(), b
                )
            )
            self.table.setCellWidget(row, 2, button)
        self.status.setText(f"{len(families)} 个资料系列。选择后点击保存。")

    def save(self, family_id, view_id, button):
        button.setEnabled(False)
        self.status.setText("正在保存默认版本…")

        def saved(choice, error):
            if self._closed:
                return
            button.setEnabled(True)
            self.status.setText(
                f"保存失败：{error}" if error else "已保存；下一个问题将使用新的默认范围。"
            )

        self.window.io.submit(
            lambda: select_default_view(self.kb, family_id, view_id),
            saved,
            kb=self.kb,
            exclusive=True,
            obsolete=lambda: self._closed,
        )

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)

    def reject(self):
        self._closed = True
        super().reject()
