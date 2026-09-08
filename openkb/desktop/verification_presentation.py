"""Exercise typography, composition, and drawers through the native workbench."""

import json

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontInfo, QInputMethodEvent, QRawFont, QTextLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel


def assert_font_face(font, text, family, weight=400, style="Regular"):
    layout = QTextLayout(text, font)
    layout.beginLayout()
    layout.createLine().setLineWidth(1000)
    layout.endLayout()
    assert layout.glyphRuns()
    for run in layout.glyphRuns():
        raw = run.rawFont()
        assert raw.familyName() == family, (text, raw.familyName())
        assert raw.styleName() == style, (weight, raw.styleName())
        # OS/2 identifies the real face's weight, even if Qt synthesizes bold.
        assert int.from_bytes(bytes(raw.fontTable("OS/2"))[4:6], "big") == weight


def assert_code_fonts(font):
    from openkb.desktop.fonts import MONO, SANS

    for text, family in (("source_code", MONO), ("中文知识", SANS)):
        assert_font_face(font, text, family)


def assert_font_weights():
    """Verify real faces after QSS inheritance, including Chinese code fallback."""
    from openkb.desktop.fonts import MONO, SANS, text_font

    for code in (False, True):
        for weight, style in ((400, "Regular"), (500, "Medium"), (700, "Bold")):
            label = QLabel("中文知识 Source_Code")
            label.setFont(text_font(20, code=code))
            label.setStyleSheet(f"font-weight: {weight};")
            label.ensurePolished()
            for text, family in (("中文知识", SANS), ("Source_Code", MONO if code else SANS)):
                assert_font_face(label.font(), text, family, weight, style)
            label.deleteLater()


def verify_presentation(window, root, wait):
    from openkb.desktop.fonts import MONO, SANS
    from openkb.desktop.verification_workbench import button

    assert QFontInfo(QApplication.font()).family() == SANS
    assert QFontInfo(window.editor.font()).family() == MONO
    assert QRawFont.fromFont(QApplication.font()).supportsCharacter(ord("知"))
    assert_code_fonts(window.editor.font())
    assert_font_weights()
    wait(lambda: "fenced_code 中文知识" in window.reader.toPlainText())
    for marker in ("inline_code 中文知识", "fenced_code 中文知识"):
        cursor = window.reader.document().find(marker)
        assert not cursor.isNull()
        assert_code_fonts(cursor.charFormat().font())
    for marker, family, weight, style in (
        ("bold_text 中文知识", SANS, 700, "Bold"),
        ("bold_code 中文知识", MONO, 700, "Bold"),
        ("italic_code", MONO, 400, "Italic"),
        ("bold_italic_code", MONO, 700, "Bold Italic"),
    ):
        cursor = window.reader.document().find(marker)
        assert not cursor.isNull(), marker
        assert_font_face(cursor.charFormat().font(), marker.split()[0], family, weight, style)
        if "中文" in marker:
            assert_font_face(cursor.charFormat().font(), "中文知识", SANS, weight, style)
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
                    "real Regular/Medium/Bold faces selected through QSS and Chinese fallback",
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
