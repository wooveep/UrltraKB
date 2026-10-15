"""Search, state filters and identity-bound native row actions."""

from pathlib import PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QPushButton,
    QTableWidgetItem,
    QToolButton,
    QWidget,
)

IDENTITY_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def focused(panel):
    item = panel.table.item(panel.table.currentRow(), 0)
    return panel.rows.get(item.data(IDENTITY_ROLE)) if item else None


def select_identity(panel, identity):
    for row in range(panel.table.rowCount()):
        if panel.table.item(row, 0).data(IDENTITY_ROLE) == identity:
            panel.table.selectRow(row)
            return True
    return False


def row_command(panel, identity, name):
    row = panel.rows.get(identity)
    if row is None or not select_identity(panel, identity):
        panel.status.setText("资料已改变，请刷新列表后重新选择。")
        return
    eligibility = row.actions.get(name)
    if eligibility and not eligibility.allowed:
        panel.status.setText(eligibility.reason)
        return
    if name == "details":
        panel.details.setPlainText(row.details)
        panel.details.setVisible(True)
    elif name == "recompile":
        panel.recompile(all_docs=False)
    elif name == "ask":
        from ultrakb.desktop.document_focus import ask_from_document

        ask_from_document(panel, identity)
    else:
        {
            "read": panel.read_source,
            "retry": panel.retry_source,
            "reprocess": panel.reprocess,
            "remove": panel.preview,
            "empty": panel.confirm_empty,
        }[name]()


def show_menu(panel, identity, button):
    row = panel.rows.get(identity)
    if row is None:
        return
    select_identity(panel, identity)
    menu = QMenu(panel)
    heading = menu.addAction(f"仅此资料：{row.document['name']}")
    heading.setEnabled(False)
    menu.addSeparator()
    for label, name in (
        ("资料详情", "details"),
        ("重试未完成项", "retry"),
        ("重新生成知识", "recompile"),
        ("预览重新处理", "reprocess"),
        ("移除资料…", "remove"),
        ("确认贡献为空…", "empty"),
    ):
        eligibility = row.actions.get(name)
        if name == "empty" and identity.kind != "source":
            continue
        text = label
        if eligibility and not eligibility.allowed:
            text += " — " + eligibility.reason
        action = menu.addAction(text)
        action.setEnabled(eligibility is None or eligibility.allowed)
        action.triggered.connect(
            lambda _checked=False, name=name: row_command(panel, identity, name)
        )
    menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
    panel._row_menu = menu


def render(panel):
    query = panel.search.text().strip().casefold()
    state = panel.state_filter.currentText()
    visible = []
    for row in panel.rows.values():
        if query and query not in row.document["name"].casefold():
            continue
        if state == "可阅读" and not row.actions["read"].allowed:
            continue
        if state == "未完成" and not row.actions["retry"].allowed:
            continue
        if state == "处理失败" and row.document.get("status") not in {
            "failed",
            "rejected",
            "blocked",
        }:
            continue
        if state == "知识待更新" and "知识待更新" not in row.status:
            continue
        visible.append(row)
    previous = focused(panel)
    panel.table.setSortingEnabled(False)
    blocked = panel.table.blockSignals(True)
    panel.table.clearContents()
    panel.table.setRowCount(len(visible))
    for index, row in enumerate(visible):
        label = row.document["name"]
        if row.document.get("location"):
            parent = PurePosixPath(row.document["location"]).parent.name
            if parent:
                label = f"{parent} / {label}"
        item = QTableWidgetItem(label)
        item.setToolTip(label)
        item.setData(Qt.ItemDataRole.UserRole, row.identity.value)
        item.setData(IDENTITY_ROLE, row.identity)
        if hasattr(panel, "selection"):
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if row.identity in panel.selection.checked
                else Qt.CheckState.Unchecked
            )
        panel.table.setItem(index, 0, item)
        kind = row.document["type"]
        panel.table.setItem(
            index, 1, QTableWidgetItem("PDF" if kind == "long_pdf" else kind.upper())
        )
        panel.table.setItem(index, 2, QTableWidgetItem(row.status))
        controls = QWidget()
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(4, 2, 4, 2)
        read = QPushButton("阅读")
        read.setAccessibleName(f"阅读 {row.document['name']}")
        read.setEnabled(row.actions["read"].allowed)
        read.clicked.connect(
            lambda _checked=False, identity=row.identity: row_command(panel, identity, "read")
        )
        more = QToolButton()
        more.setText("更多")
        more.setAccessibleName(f"更多 {row.document['name']}")
        more.clicked.connect(
            lambda _checked=False, identity=row.identity, button=more: show_menu(
                panel, identity, button
            )
        )
        layout.addWidget(read)
        ask = QPushButton("提问")
        ask.setAccessibleName(f"关注 {row.document['name']} 提问")
        ask.setEnabled(row.actions["ask"].allowed)
        ask.setToolTip(row.actions["ask"].reason or "优先检索此资料，仍可使用本库其他资料。")
        ask.clicked.connect(
            lambda _checked=False, identity=row.identity: row_command(panel, identity, "ask")
        )
        layout.addWidget(ask)
        layout.addWidget(more)
        panel.table.setCellWidget(index, 3, controls)
        controls.ensurePolished()
        layout.activate()
        panel.table.setColumnWidth(
            3, max(panel.table.columnWidth(3), controls.minimumSizeHint().width() + 4)
        )
        panel.table.setRowHeight(
            index,
            max(64, controls.sizeHint().height() + 16, controls.minimumSizeHint().height() + 16),
        )
    panel.table.setSortingEnabled(True)
    if previous:
        select_identity(panel, previous.identity)
    panel.table.blockSignals(blocked)
    if hasattr(panel, "selection"):
        from ultrakb.desktop.document_selection import update

        panel.selection.visible = tuple(row.identity for row in visible)
        update(panel)
    count = len(visible)
    panel.status.setText(
        f"显示 {count} / {len(panel.rows)} 份资料。选中行可查看操作资格，更多提供维护操作。"
        if count
        else "没有符合筛选条件的资料；可清除搜索或切换状态。"
        if panel.rows
        else "暂无资料。请使用上方“导入资料”添加文件、目录或网址。"
    )
    if notice := getattr(panel, "_navigation_notice", None):
        panel.status.setText(notice)


def context_menu(panel, position):
    item = panel.table.itemAt(position)
    if item is None:
        return
    name = panel.table.item(item.row(), 0)
    identity = name.data(IDENTITY_ROLE)
    # Reading and the context menu never mutate the checkbox intent.
    controls = panel.table.cellWidget(item.row(), 3)
    button = controls.findChild(QToolButton)
    show_menu(panel, identity, button)


def selection_changed(panel):
    panel.invalidate()
    row = focused(panel)
    if getattr(panel, "_navigation_notice", None):
        if row and row.identity == getattr(panel, "_navigation_identity", None):
            panel.status.setText(panel._navigation_notice)
            return
        panel._navigation_generation += 1
        panel._navigation_notice = None
    if row:
        panel.details.setPlainText(row.details)
        reasons = [
            action.reason
            for name, action in row.actions.items()
            if name in {"read", "ask", "retry", "recompile"}
            and not action.allowed
            and action.reason
        ]
        panel.status.setText(
            f"{row.document['name']} · {row.status}"
            + (f"\n原输入位置：{row.document['location']}" if row.document.get("location") else "")
            + ("\n" + "\n".join(reasons) if reasons else "")
        )
