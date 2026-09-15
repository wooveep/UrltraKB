"""Single-click browsing and responsive images through real native widgets."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def verify_reading(window, root, wait):
    tree = window.pages
    window.shell.navigate("知识")
    tree.expandAll()

    def item(path):
        return next(
            node
            for node in tree.findItems(
                "*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive
            )
            if node.data(0, Qt.ItemDataRole.UserRole) == path + ".md"
        )

    def click(path):
        node = item(path)
        tree.scrollToItem(node)
        QApplication.processEvents()
        QTest.mouseClick(
            tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualItemRect(node).center()
        )

    target = "concepts/原生阅读"
    click(target)
    wait(lambda: window.page is not None and window.page.path == target)
    wait(lambda: "fenced_code 中文知识" in window.reader.toPlainText())
    assert window.tabs.currentIndex() == 0
    assert tree.currentItem() is item(target)
    original = window.editor.toPlainText()
    window.tabs.setCurrentIndex(1)
    window.editor.setPlainText(original + "\n目录切换保留草稿。")
    click(target)
    assert window.tabs.currentIndex() == 0, "clicking the selected page must return to reading"
    click("index")
    wait(lambda: window.page.path == "index")
    click(target)
    wait(lambda: window.page.path == target)
    assert "目录切换保留草稿" in window.editor.toPlainText()
    window.editor.setPlainText(original)

    # The last selection wins even if previous page reads are still queued.
    click("index")
    click(target)
    wait(lambda: window.page.path == target and not window.io._callbacks)
    assert tree.currentItem() is item(target)

    from openkb.desktop.reader import MarkdownView

    view = MarkdownView(window)
    view.setWindowFlags(Qt.WindowType.Window)
    view.resize(680, 520)
    view.show()
    figure = QImage(1800, 900, QImage.Format.Format_RGB32)
    figure.fill(QColor("#365bd6"))
    figure.save(str(root / "wide-figure.png"))
    document = (
        "# 阅读排版\n\n中文段落与 `code`。\n\n"
        "![宽幅图片](wide-figure.png)\n\n"
        "| 项目 | 结果 |\n| --- | --- |\n| 表格 | 清晰可读 |\n\n"
        + "\n\n".join(f"## 小节 {i}\n\n连续阅读，保持当前滚动位置。" for i in range(30))
    )
    try:
        view.show_markdown(document, root)
        wait(lambda: "小节 29" in view.toPlainText())

        def image_width():
            return view.document().find("\ufffc").charFormat().toImageFormat().width()

        wide = image_width()
        assert 0 < wide < 680
        view.resize(360, 520)
        QApplication.processEvents()
        assert image_width() < wide
        assert view.horizontalScrollBar().maximum() == 0
        view.resize(1280, 520)
        QApplication.processEvents()
        assert image_width() <= 900
        view.verticalScrollBar().setValue(500)
        previous_generation = view._generation
        view.set_presentation(dark=True, scale=1)
        wait(lambda: view._generation > previous_generation and view.rendering_stopped())
        QApplication.processEvents()
        assert abs(view.verticalScrollBar().value() - 500) <= 2
        view.resize(360, 520)
        QApplication.processEvents()
        assert view.width() == 360, "wide reading margins must not prevent window shrinking"
        view.verticalScrollBar().setValue(0)
        view.grab().save(str(root / "reading-narrow.png"))
    finally:
        view.stop_rendering()
        wait(view.rendering_stopped)
        view.close()
    window.shell.navigate("概览")
    window.activateWindow()
    QApplication.processEvents()
