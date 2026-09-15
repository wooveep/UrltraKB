"""Find acceptance prompts directly in Qt's visible top-level widgets."""

import os

from PySide6.QtWidgets import QApplication, QMessageBox

_observations: dict[str, int] = {}


def message_box(text: str) -> QMessageBox | None:
    boxes = [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QMessageBox) and widget.isVisible()
    ]
    if os.environ.get("URLTRAKB_VERIFY_TIMEOUT_TRACE") == "1":
        count = _observations.get(text, 0)
        _observations[text] = count + 1
        if count < 2 or count % 50 == 0:
            print(
                "[DEBUG-mac-dialog]",
                text,
                "active=",
                type(QApplication.activeModalWidget()).__name__,
                "visible=",
                [(box.text(), box.informativeText()) for box in boxes],
                flush=True,
            )
    return next((box for box in boxes if text in box.text() or text in box.informativeText()), None)
