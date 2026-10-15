"""Keep checkbox intent separate from keyboard focus, row order and filters."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ultrakb.desktop.document_rows import IDENTITY_ROLE, render
from ultrakb.desktop.flow_layout import FlowLayout


class DocumentSelection:
    def __init__(self):
        self.checked = set()
        self.visible = ()

    def reconcile(self, available):
        removed = self.checked - set(available)
        self.checked.difference_update(removed)
        return removed


def install(panel, layout):
    panel.selection = DocumentSelection()
    panel.bulk_bar = QWidget()
    body = QVBoxLayout(panel.bulk_bar)
    body.setContentsMargins(0, 0, 0, 0)
    panel.bulk_count = QLabel()
    panel.bulk_count.setWordWrap(True)
    body.addWidget(panel.bulk_count)
    row = FlowLayout()
    body.addLayout(row)
    panel.bulk_recompile = QPushButton("重新生成知识…")
    panel.bulk_recompile.setAccessibleName("批量重新生成知识")
    panel.bulk_recompile.clicked.connect(panel.recompile_checked)
    row.addWidget(panel.bulk_recompile)
    panel.bulk_retry = QPushButton("重试未完成项…")
    panel.bulk_retry.setAccessibleName("批量重试未完成项")
    panel.bulk_retry.clicked.connect(panel.retry_checked)
    row.addWidget(panel.bulk_retry)
    panel.bulk_remove = QPushButton("移除所选资料…")
    panel.bulk_remove.setAccessibleName("批量移除所选资料")
    panel.bulk_remove.clicked.connect(panel.remove_checked)
    row.addWidget(panel.bulk_remove)
    panel.bulk_remove_note = QLabel()
    panel.bulk_remove_note.setWordWrap(True)
    body.addWidget(panel.bulk_remove_note)
    panel.bulk_reasons = QPushButton("查看不适用原因")
    panel.bulk_reasons.clicked.connect(lambda: show_reasons(panel))
    row.addWidget(panel.bulk_reasons)
    clear = QPushButton("取消选择")
    clear.clicked.connect(lambda: clear_selection(panel))
    row.addWidget(clear)
    layout.addWidget(panel.bulk_bar)
    panel.bulk_bar.hide()
    panel.table.itemChanged.connect(lambda item: item_changed(panel, item))
    select_all = QShortcut(QKeySequence.StandardKey.SelectAll, panel.table)
    select_all.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    select_all.activated.connect(lambda: select_visible(panel))
    panel.select_all_shortcut = select_all


def update(panel):
    selected = [
        panel.rows[identity] for identity in panel.selection.checked if identity in panel.rows
    ]
    applicable = sum(row.actions["recompile"].allowed for row in selected)
    retry_count = sum(row.actions["retry"].allowed for row in selected)
    panel.bulk_bar.setVisible(bool(selected))
    panel.bulk_count.setText(
        f"已选 {len(selected)} 项 · {applicable} 项可重新生成 · "
        f"{len(selected) - applicable} 项不适用"
        + f"\n{retry_count} 项可重试未完成内容；已完成项保留。"
    )
    panel.bulk_recompile.setEnabled(applicable > 0 and panel._task is None)
    panel.bulk_retry.setEnabled(retry_count > 0 and panel._task is None)
    panel.bulk_remove.setEnabled(
        bool(selected)
        and panel._task is None
        and all(row.actions["remove"].allowed for row in selected)
    )
    panel.bulk_remove_note.setText(
        "新旧资料的移除后果将分别确认。"
        if any(row.identity.kind == "legacy" for row in selected)
        else ""
    )
    panel.bulk_reasons.setVisible(applicable != len(selected))


def item_changed(panel, item):
    if item.column() != 0:
        return
    identity = item.data(IDENTITY_ROLE)
    if identity is None:
        return
    if item.checkState() == Qt.CheckState.Checked:
        panel.selection.checked.add(identity)
    else:
        panel.selection.checked.discard(identity)
    update(panel)


def select_visible(panel):
    panel.selection.checked.update(panel.selection.visible)
    render(panel)


def clear_selection(panel):
    panel.selection.checked.clear()
    render(panel)


def show_reasons(panel):
    panel.details.setPlainText(
        "\n".join(
            f"{panel.rows[identity].document['name']}：{panel.rows[identity].actions['recompile'].reason}"
            for identity in sorted(panel.selection.checked)
            if identity in panel.rows and not panel.rows[identity].actions["recompile"].allowed
        )
    )
    panel.details.show()
