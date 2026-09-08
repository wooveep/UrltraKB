"""Acceptance through the actual native workbench and its user-facing controls."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel


def button(window, name):
    return next(
        child
        for child in window.findChildren(QAbstractButton)
        if child.accessibleName() == name and child.isVisible()
    )


def on_page(window, name):
    return any(label.isVisible() and label.text() == name for label in window.findChildren(QLabel))


def verify_workbench(window, first, other, root, wait):
    assert window.windowTitle() == "UrltraKB"
    assert not window.windowIcon().isNull()
    button(window, "新建知识库")
    button(window, "打开知识库")
    window.open_knowledge_base(first)
    wait(lambda: window.page is not None)
    assert on_page(window, "概览")
    for name in ("资料", "知识", "对话", "产物", "任务", "设置", "概览"):
        QTest.mouseClick(button(window, name), Qt.MouseButton.LeftButton)
        wait(lambda: on_page(window, name))
    button(window, "资料").click()
    from openkb.desktop.documents import DocumentsDialog

    wait(lambda: any(p.isVisible() for p in window.findChildren(DocumentsDialog)))
    documents = next(p for p in window.findChildren(DocumentsDialog) if p.isVisible())
    QTest.keyClick(documents, Qt.Key.Key_Escape)
    assert documents.isVisible(), "Escape must not dismiss an embedded workbench page"
    window.resize(1366, 768)
    toggle = button(window, "收起导航")
    QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
    assert button(window, "概览").toolTip() == "概览"
    expand = button(window, "展开导航")
    expand.setFocus(Qt.FocusReason.TabFocusReason)
    assert expand.hasFocus()
    QTest.keyClick(expand, Qt.Key.Key_Space)
    window.resize(900, 650)
    wait(lambda: button(window, "展开导航").isVisible())
    window.resize(1366, 768)
    wait(lambda: button(window, "收起导航").isVisible())
    QTest.mouseClick(button(window, "设置"), Qt.MouseButton.LeftButton)
    theme = next(c for c in window.findChildren(QComboBox) if c.accessibleName() == "应用主题")
    assert theme.currentText() == "跟随系统"
    theme.setCurrentText("深色")
    wait(lambda: window.palette().color(QPalette.ColorRole.Window).lightness() < 128)
    assert window.editor.palette().color(QPalette.ColorRole.Base).lightness() < 128
    theme.setCurrentText("浅色")
    wait(lambda: window.palette().color(QPalette.ColorRole.Window).lightness() > 128)
    verify_settings_scopes(window, wait)

    # Keep a draft intact while opening secondary navigation and changing themes.
    window.open_page("concepts/原生阅读")
    wait(lambda: window.page is not None and window.page.path == "concepts/原生阅读")
    wait(lambda: "与文字保持基线" in window.reader.toPlainText())
    window.tabs.setCurrentIndex(1)
    window.editor.setPlainText(window.editor.toPlainText() + "\n尚未保存的草稿。")
    for label in ("知识目录", "来源与链接", "来源与链接", "知识目录"):
        QTest.mouseClick(button(window, label), Qt.MouseButton.LeftButton)
        assert "尚未保存的草稿" in window.editor.toPlainText()
    assert window.editor.isVisible()

    # Task submission preserves this page, even when another same-named KB is opened.
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.pages import read_page
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.requests import SavePage

    twin = root / "另一路径" / first.name
    initialize_kb(twin, seed_environment=False)
    with kb_ingest_lock(first / ".openkb"):
        button(window, "保存正文").click()
        task = window.manager.tasks()[-1]
        wait(lambda: window.manager.get(task.id).state == "waiting")
        assert on_page(window, "知识") and window.editor.isVisible()
        wait(lambda: "1 运行" in button(window, "查看任务").text())
        window.open_knowledge_base(twin)
        wait(lambda: window.kb == twin and on_page(window, "概览"))
    wait(lambda: window.manager.get(task.id).state == "completed")
    assert "尚未保存的草稿" in read_page(first, "concepts/原生阅读").body
    button(window, "查看任务").click()
    wait(lambda: window.task_table.rowCount() > 0)
    window.task_table.selectRow(0)
    assert str(first) in window.workspaces.task_owner.text()
    assert window.kbs.toolTip() == str(twin)
    assert not (twin / "wiki/concepts/原生阅读.md").exists()

    # A conflict provides a global attention entry without destroying the current draft.
    window.open_knowledge_base(first)
    wait(lambda: window.kb == first and window.page is not None)
    window.open_page("concepts/原生阅读")
    wait(lambda: window.page is not None and window.page.path == "concepts/原生阅读")
    window.manager.submit(first, [SavePage("concepts/原生阅读", "Must not overwrite", "stale")])
    wait(lambda: any(t.state == "failed" for t in window.manager.tasks()))
    wait(lambda: "1 需关注" in button(window, "查看任务").text())
    assert on_page(window, "知识")
    assert "Must not overwrite" not in read_page(first, "concepts/原生阅读").body

    verify_history_submission(window, first, wait)

    # System palette changes are supplied at the Qt platform boundary.
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QDialog

    button(window, "设置").click()
    theme.setCurrentText("跟随系统")
    for color, dark in (("#111111", True), ("#fafafa", False)):
        palette = QPalette(QApplication.palette())
        palette.setColor(QPalette.ColorRole.Window, QColor(color))
        QApplication.setPalette(palette)
        wait(lambda: (window.palette().color(QPalette.ColorRole.Window).lightness() < 128) == dark)
    theme.setCurrentText("深色")
    dialog = QDialog(window)
    dialog.show()
    wait(lambda: dialog.isVisible())
    assert dialog.palette().color(QPalette.ColorRole.Window).lightness() < 128
    dialog.close()

    capture_workbench(window, theme, root, wait)
    # Leave explicit choices for a fresh-process check of this isolated Qt profile.
    window.resize(1366, 768)
    button(window, "设置").click()
    theme.setCurrentText("深色")
    button(window, "收起导航").click()


def capture_workbench(window, theme, root, wait):
    """Capture actual widgets at logical desktop/laptop/narrow dimensions."""
    from PySide6.QtWidgets import QApplication

    pages = ("概览", "资料", "知识", "对话", "产物", "任务", "设置")
    for theme_name, suffix in (("浅色", "light"), ("深色", "dark")):
        theme.setCurrentText(theme_name)
        for width, height in ((1366, 768), (1920, 1080), (900, 650)):
            window.resize(width, height)
            for number, name in enumerate(pages):
                window.shell.navigate(name)
                if name == "知识":
                    window.tabs.setCurrentIndex(0)
                    wait(lambda: "与文字保持基线" in window.reader.toPlainText())
                if name == "设置":
                    from openkb.desktop.settings import SettingsDialog

                    wait(
                        lambda: any(
                            p.isVisible() and p.form.isEnabled()
                            for p in window.findChildren(SettingsDialog)
                        )
                    )
                QApplication.processEvents()
                assert window.size().width() == width and window.size().height() == height
                assert window.shell.title.isVisible()
                assert window.grab().save(str(root / f"{number}-{suffix}-{width}.png"))


def verify_appearance_restart(window):
    window.resize(1366, 768)
    assert button(window, "展开导航").isVisible()
    button(window, "设置").click()
    theme = next(c for c in window.findChildren(QComboBox) if c.accessibleName() == "应用主题")
    assert theme.currentText() == "深色"
    assert window.palette().color(QPalette.ColorRole.Window).lightness() < 128


def management_page(window, kb, name, panel_type, wait):
    """Reuse business scenarios through the visible workbench page, not a second dialog."""
    window.open_knowledge_base(kb)
    wait(
        lambda: window.kb == kb
        and window.page is not None
        and not window.statusBar().currentMessage().startswith("正在打开")
    )
    button(window, name).click()
    if name == "对话":
        button(window, "对话历史").click()
    wait(lambda: any(panel.isVisible() for panel in window.findChildren(panel_type)))
    return next(panel for panel in window.findChildren(panel_type) if panel.isVisible())


def verify_settings_scopes(window, wait):
    from PySide6.QtWidgets import QDialogButtonBox

    from openkb.desktop.settings import SettingsDialog

    tabs = window.workspaces.settings_tabs

    def select(index):
        tabs.setCurrentIndex(index)
        wait(lambda: any(p.isVisible() for p in window.findChildren(SettingsDialog)))
        panel = next(p for p in window.findChildren(SettingsDialog) if p.isVisible())
        wait(lambda: panel.form.isEnabled())
        return panel

    def save(panel):
        panel.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        wait(lambda: panel.form.isEnabled())

    kb = select(1)
    kb.fields["language"].action.setCurrentIndex(2)
    save(kb)
    defaults = select(0)
    defaults.fields["language"].action.setCurrentIndex(1)
    defaults.fields["language"].text.setText("Chinese")
    save(defaults)
    kb = select(1)
    assert kb.fields["language"].text.placeholderText() == "Chinese"
    assert kb.fields["language"].source.text() == "全局"
    kb.fields["language"].action.setCurrentIndex(1)
    kb.fields["language"].text.setText("Pending edit")
    select(0)
    kb = select(1)
    assert kb.fields["language"].text.text() == "Pending edit"
    kb.fields["language"].action.setCurrentIndex(0)


def verify_history_submission(window, kb, wait):
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.records import TERMINAL

    button(window, "对话").click()
    button(window, "对话历史").click()
    wait(lambda: not window.chat.isVisible())
    window.question.setPlainText("排队显示验证")
    # Hold the real execution boundary and cancel before release: no model request.
    with kb_ingest_lock(kb / ".openkb"):
        button(window, "发送").click()
        task_id = window.manager.tasks()[-1].id
        try:
            assert on_page(window, "对话") and window.chat.isVisible()
            assert "已排队" in window.chat.toPlainText()
        finally:
            window.manager.stop(task_id)
    wait(lambda: window.manager.get(task_id).state in TERMINAL)
