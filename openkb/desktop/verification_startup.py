"""Verify the first visible frame before any manual appearance change."""

import json
import time

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontInfo, QRawFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget


def verify_startup(app, root, mode):
    from openkb.desktop.fonts import MONO, SANS
    from openkb.desktop.window import Workbench

    preferences = QSettings(QSettings.defaultFormat(), QSettings.UserScope, "OpenKB", "OpenKB")
    if mode != "system":
        preferences.setValue("appearance/theme", mode)
        preferences.sync()
    window = Workbench(history_dir=root / "tasks")
    try:
        window.show()
        QTest.qWait(250)
        widgets = [window, *(w for w in window.findChildren(QWidget) if w.isVisible())]
        before = [w.font().toString() for w in widgets]
        assert window.grab().save(str(root / "first-frame.png"))
        for widget in widgets:
            family = QFontInfo(widget.font()).family()
            assert family in (SANS, MONO), (type(widget).__name__, widget.objectName(), family)
            if family == SANS:
                assert QRawFont.fromFont(widget.font()).supportsCharacter(ord("知"))
        assert window.theme.currentData() == mode
        for theme in ("dark" if mode != "dark" else "light", mode):
            window.theme.setCurrentIndex(window.theme.findData(theme))
            QTest.qWait(50)
        assert before == [w.font().toString() for w in widgets]
        (root / "startup.json").write_text(
            json.dumps(
                {"theme": mode, "widgets_checked": len(widgets), "first_frame_fonts_stable": True},
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0
    finally:
        window.request_quit()
        deadline = time.monotonic() + 10
        while window.timer.isActive() and time.monotonic() < deadline:
            QTest.qWait(20)
        assert not window.timer.isActive(), "Startup verification did not shut down"
