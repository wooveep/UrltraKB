"""Opt-in actual Qt navigation, statistics, and directory-delete acceptance."""

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from openkb import config
from openkb.application.knowledge_bases import initialize_kb
from openkb.application.pages import read_page
from openkb.desktop.knowledge_bases import KnowledgeBasesDialog
from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.runtime.requests import SavePage


def verify_catalog(window, output, wait_until):
    first = config.kb_root_dir() / "管理验证库"
    other = output / "另一路径/管理验证库"
    for root in (first, other):
        initialize_kb(root, seed_environment=False)
        with kb_ingest_lock(root / ".openkb"):
            atomic_write_text(root / "wiki/concepts/相关.md", "# 相关页面\n")
    with kb_ingest_lock(first / ".openkb"):
        atomic_write_text(first / "wiki/sources/paper.md", "# 原始来源\n原始资料正文。")
        atomic_write_text(
            first / "wiki/concepts/笔记.md",
            "---\nsources: [paper, missing]\n---\n# 关联阅读\n[[concepts/相关]]",
        )
        atomic_write_text(first / "wiki/index.md", "# 首页\n[[concepts/笔记]]")
        atomic_write_text(first / "raw/nested/one.bin", "Not an accepted watch input")
        atomic_write_text(first / "raw/two.bin", "Another raw file")
    window.open_knowledge_base(first)
    wait_until(lambda: window.kb == first and window.page is not None)
    window.open_page("concepts/笔记")
    wait_until(lambda: window.page is not None and window.page.path == "concepts/笔记")
    wait_until(lambda: "关联阅读" in window.reader.toPlainText())
    sources = window.page_context.topLevelItem(0)
    assert sources.childCount() == 2
    assert "未找到页面" in sources.child(1).text(0)
    source_item = sources.child(0)
    window.page_context.scrollToItem(source_item)
    QTest.mouseClick(
        window.page_context.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.page_context.visualItemRect(source_item).center(),
    )
    QTest.mouseDClick(
        window.page_context.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.page_context.visualItemRect(source_item).center(),
    )
    wait_until(lambda: window.page is not None and window.page.path == "sources/paper")
    wait_until(lambda: "原始资料正文" in window.reader.toPlainText())
    window.open_page("concepts/笔记")
    wait_until(lambda: window.page is not None and window.page.path == "concepts/笔记")
    wait_until(lambda: "关联阅读" in window.reader.toPlainText())
    window.grab().save(str(output / "native-page-context.png"))

    dialog = KnowledgeBasesDialog(window)
    dialog.show()

    def row_for(root):
        return next(
            (
                row
                for row in range(dialog.table.rowCount())
                if dialog.table.item(row, 1).text() == str(root)
            ),
            None,
        )

    wait_until(lambda: row_for(first) is not None and row_for(other) is not None)
    dialog.table.selectRow(row_for(first))
    wait_until(lambda: "原始文件：2" in dialog.details.toPlainText())
    assert "概念：2" in dialog.details.toPlainText()
    assert window.kbs.findData(str(first)) >= 0 and window.kbs.findData(str(other)) >= 0
    dialog.grab().save(str(output / "native-kb-statistics.png"))
    watch = window.watch_registry.start(first)
    wait_until(lambda: watch.view().scans >= 1)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with kb_ingest_lock(first / ".openkb"):
            ready.set()
            assert release.wait(45)

    holder = threading.Thread(target=hold)
    holder.start()

    def confirm():
        question = QApplication.activeModalWidget()
        if not isinstance(question, QInputDialog):
            QTimer.singleShot(20, confirm)
            return
        question.setTextValue(first.name)
        question.accept()

    try:
        assert ready.wait(5)
        page = read_page(other, "concepts/相关")
        queued = window.manager.submit(
            first, [SavePage(page.path, "stale queued edit", page.version)]
        )
        QTimer.singleShot(20, confirm)
        dialog.buttons[2].click()
        wait_until(lambda: dialog._deleting)
        independent = window.manager.submit(
            other, [SavePage(page.path, "independent", page.version)]
        )
        wait_until(lambda: window.manager.get(independent).state == "completed")
        assert first.is_dir()
        dialog.cancel_button.click()
        wait_until(lambda: not dialog._deleting)
        assert "已撤回" in dialog.status.text()
        wait_until(lambda: window.manager.get(queued).processes_reaped)
        assert window.manager.get(queued).state == "stopped"
        assert watch.join(5)
        assert "stale queued edit" not in (first / "wiki/concepts/相关.md").read_text(
            encoding="utf-8"
        )
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()
    wait_until(lambda: row_for(first) is not None)
    dialog.table.selectRow(row_for(first))
    QTimer.singleShot(20, confirm)
    dialog.buttons[2].click()
    wait_until(lambda: not first.exists() and not dialog._deleting)
    assert window.kb is None
    assert other.is_dir() and read_page(other, "concepts/相关").body == "independent"
    assert window.manager.get(queued).state == "stopped"
    dialog.grab().save(str(output / "native-kb-deleted.png"))
    dialog.accept()
