"""Exercise populated native tables, including header geometry and narrow layouts."""

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def verify_tables(window, kb, root, wait):
    from openkb.desktop.documents import DocumentsDialog
    from openkb.desktop.verification_workbench import management_page
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock

    names = (
        "OCloudView 云平台操作与维护手册.docx",
        "V10.3 版本发布说明与升级指南.pdf",
        "【重要】瑞光项目交付与验收要求.docx",
        "常见问题排查与处理方法.md",
        "简易巡检及日常维护操作说明.pdf",
        "OCloudView 管理员配置参考手册.docx",
        "跨团队知识整理与资料归档规范（2026 年修订版）.pdf",
    )
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_json(
            kb / ".openkb/hashes.json",
            {
                f"table-{index}": {"name": name, "doc_name": f"table-{index}", "type": "short"}
                for index, name in enumerate(names)
            },
        )
        atomic_write_text(kb / "wiki/sources/table-0.md", "资料原文。")
        atomic_write_text(kb / "wiki/summaries/table-0.md", "# 资料摘要\n")
    panel = management_page(window, kb, "资料", DocumentsDialog, wait)
    wait(lambda: panel.table.rowCount() == len(names))
    table = panel.table
    for theme, label in (("浅色", "light"), ("深色", "dark")):
        window.theme.setCurrentText(theme)
        for width, height in ((1320, 720), (900, 650), (720, 600)):
            window.resize(width, height)
            QTest.qWait(30)
            window.grab().save(str(root / f"documents-{label}-{width}.png"))
            assert_header_alignment(table)
            assert table.columnWidth(0) >= table.viewport().width() * 0.45, (
                f"Document name gets only {table.columnWidth(0)}/{table.viewport().width()} pixels"
            )
            assert table.horizontalScrollBar().maximum() == 0
            assert window.workspaces.panels["资料"][1].horizontalScrollBar().maximum() == 0
            assert window.workspaces.panels["资料"][1].verticalScrollBar().maximum() == 0
            assert table.rowHeight(0) >= table.fontMetrics().height() + 16
            assert table.item(0, 0).toolTip() == names[0]
            assert not panel.details.isVisible() and not panel.removal_panel.isVisible()
            for button in (panel.review_button, panel.recompile_selected, panel.removal_toggle):
                assert button.width() >= button.sizeHint().width(), button.text()
    assert not panel.review_button.isEnabled() and not panel.recompile_selected.isEnabled()
    QTest.mouseClick(
        table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=table.visualRect(table.model().index(0, 0)).center(),
    )
    assert panel.review_button.isEnabled() and panel.recompile_selected.isEnabled()
    assert "已选择 1 份" in panel.selection_status.text()
    selection = table.selectionModel()
    selection.select(
        table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert not panel.review_button.isEnabled() and not panel.removal_toggle.isEnabled()
    assert panel.recompile_selected.isEnabled()
    table.clearSelection()
    table.selectRow(0)
    QTest.mouseClick(panel.removal_toggle, Qt.MouseButton.LeftButton)
    assert panel.removal_panel.isVisible()
    QTest.mouseClick(panel.preview_button, Qt.MouseButton.LeftButton)
    wait(lambda: panel.confirm_button.isEnabled())
    assert panel.details.isVisible() and "table-0" in panel.details.toPlainText()
    panel.keep_raw.setChecked(True)
    assert not panel.confirm_button.isEnabled() and not panel.details.isVisible()
    table.clearSelection()
    assert panel.removal_toggle.isEnabled() and not panel.preview_button.isEnabled()
    panel.removal_toggle.click()
    assert not panel.removal_panel.isVisible()

    # A long inventory scrolls inside the table and keeps its header aligned.
    table.setRowCount(80)
    table.scrollToBottom()
    QApplication.processEvents()
    assert table.verticalScrollBar().maximum() > 0
    assert_header_alignment(table, row=79)
    panel.reload()
    wait(lambda: table.rowCount() == len(names))
    window.resize(1320, 720)
    window.theme.setCurrentText("浅色")
    table.selectRow(0)
    QTest.qWait(30)
    window.grab().save(str(root / "documents-selected.png"))

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_json(kb / ".openkb/hashes.json", {})
    panel.reload()
    wait(lambda: table.rowCount() == 0)
    assert "暂无资料" in panel.status.text()
    assert not panel.recompile_all.isEnabled()
    assert not panel.review_button.isEnabled()
    from openkb.desktop.verification_removal import verify_removal

    verify_removal(window, kb, wait)


def assert_header_alignment(table, row=0):
    header = table.horizontalHeader()
    offsets = [
        header.viewport().mapToGlobal(QPoint(header.sectionViewportPosition(column), 0)).x()
        - table.viewport()
        .mapToGlobal(table.visualRect(table.model().index(row, column)).topLeft())
        .x()
        for column in range(table.columnCount())
    ]
    assert max(abs(offset) for offset in offsets) <= 1, f"Header/cell offsets: {offsets}"
