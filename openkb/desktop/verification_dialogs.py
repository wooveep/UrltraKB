"""Observe acceptance dialogs only after their modal event loop has started."""

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox


def visible_dialogs() -> list[QDialog]:
    # This driver pumps events itself, without an outer QApplication.exec().
    # Cocoa may dispatch timers during show(), before QDialog.exec registers its
    # event loop. Closing a prompt then leaves exec() waiting forever afterwards.
    if QThread.currentThread().loopLevel() == 0:
        return []
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog) and widget.isVisible()
    ]


def message_box(text: str) -> QMessageBox | None:
    return next(
        (
            box
            for box in visible_dialogs()
            if isinstance(box, QMessageBox)
            and (text in box.text() or text in box.informativeText())
        ),
        None,
    )
