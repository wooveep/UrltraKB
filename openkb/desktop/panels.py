"""Management forms may be hosted as pages without modal dismissal semantics."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog


class ManagementPanel(QDialog):
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and not self.isWindow():
            event.ignore()
            return
        super().keyPressEvent(event)
