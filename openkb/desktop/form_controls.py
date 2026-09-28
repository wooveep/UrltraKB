"""Native forms scroll inside their content area without accidental selection changes."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLayout, QScrollArea


class FocusComboBox(QComboBox):
    """An unfocused wheel belongs to the surrounding form, not this choice."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def scroll_form(content):
    """Constrain a long form while allowing conditional fields to change its extent."""
    scroll = QScrollArea()
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    if content.layout() is not None:
        content.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(content)
    return scroll
