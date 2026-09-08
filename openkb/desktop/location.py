"""Single-line locations with selectable text and full-value inspection."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy


class LocationLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setText(text)

    def setText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self._elide()

    def _elide(self):
        super().setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideMiddle, self.width()
            )
        )

    def resizeEvent(self, event):
        self._elide()
        super().resizeEvent(event)
