"""Exercise typography, composition, and drawers through the native workbench."""

import json

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontInfo, QInputMethodEvent, QRawFont, QTextLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def assert_code_fonts(font):
    for text, family in (("source_code", "Source Code Pro"), ("中文知识", "Source Han Sans CN VF")):
        layout = QTextLayout(text, font)
        layout.beginLayout()
        layout.createLine().setLineWidth(1000)
        layout.endLayout()
        assert {run.rawFont().familyName() for run in layout.glyphRuns()} == {family}
    assert font.variableAxisValue(QFont.Tag("wght")) == 400


def verify_presentation(window, root, wait):
    from openkb.desktop.fonts import MONO, SANS
    from openkb.desktop.verification_workbench import button

    assert QFontInfo(QApplication.font()).family() == SANS
    assert QFontInfo(window.editor.font()).family() == MONO
    assert QRawFont.fromFont(QApplication.font()).supportsCharacter(ord("知"))
    assert_code_fonts(window.editor.font())
    wait(lambda: "fenced_code 中文知识" in window.reader.toPlainText())
    for marker in ("inline_code 中文知识", "fenced_code 中文知识"):
        cursor = window.reader.document().find(marker)
        assert not cursor.isNull()
        assert_code_fonts(cursor.charFormat().font())
    window.shell.navigate("对话")
    window.question.setPlainText("保留中文草稿与 source_code()")
    window.question.setFocus()
    drawer = window.workspaces.history_drawer
    samples = []
    drawer.opacity.opacityChanged.connect(samples.append)
    button(window, "对话历史").click()
    wait(lambda: drawer.opacity.opacity() == 1)
    assert any(0 < value < 1 for value in samples)
    assert window.question.isVisible() and window.ask_button.isVisible()
    assert window.grab().save(str(root / "history-drawer.png"))
    QTest.keyClick(button(window, "关闭对话历史"), Qt.Key.Key_Escape)
    wait(lambda: not drawer.isVisible())
    assert window.question.hasFocus()
    assert window.question.toPlainText() == "保留中文草稿与 source_code()"
    # Reverse an in-flight transition, then close through the surrounding surface.
    for _ in range(3):
        button(window, "对话历史").click()
        QTest.qWait(25)
    wait(lambda: drawer.opacity.opacity() == 1)
    window.resize(900, 650)
    QApplication.processEvents()
    assert drawer.panel.geometry().right() <= drawer.width()
    assert window.question.isVisible()
    QTest.mouseClick(drawer, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    wait(lambda: not drawer.isVisible())
    window.resize(1366, 768)

    # Shift+Enter and an IME's composition-confirmation key must not submit work.
    before = len(window.manager.tasks())
    window.question.clear()
    QTest.keyClick(window.question, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert window.question.toPlainText() == "\n"
    window.question.setPlainText("中文输入")
    QApplication.sendEvent(window.question, QInputMethodEvent("拼音", []))
    QTest.keyClick(window.question, Qt.Key.Key_Return)
    assert len(window.manager.tasks()) == before
    QApplication.sendEvent(window.question, QInputMethodEvent())
    window.question.clear()

    window.shell.navigate("知识")
    window.tabs.setCurrentIndex(1)
    draft = window.editor.toPlainText()
    button(window, "来源与链接").click()
    wait(lambda: window.workspaces.context_drawer.opacity.opacity() == 1)
    QTest.keyClick(button(window, "关闭来源与链接"), Qt.Key.Key_Escape)
    wait(lambda: not window.page_context.isVisible())
    assert window.editor.toPlainText() == draft
    (root / "presentation.json").write_text(
        json.dumps(
            {
                "ui_font": QApplication.font().toString(),
                "code_font": window.editor.font().toString(),
                "drawer_intermediate_frames": len([v for v in samples if 0 < v < 1]),
                "checks": [
                    "bundled Source Han Sans and Source Code Pro selected",
                    "fade, reversal, outside click, Escape and focus restoration",
                    "narrow drawer and persistent message/page drafts",
                    "Shift+Enter and IME confirmation do not submit work",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
