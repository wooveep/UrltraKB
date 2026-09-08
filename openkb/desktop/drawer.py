"""An interruptible, local drawer that keeps the underlying workspace intact."""

from PySide6.QtCore import QEasingCurve, QEvent, Qt, QVariantAnimation, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class Drawer(QWidget):
    openedChanged = Signal(bool)

    def __init__(self, host, title, *, width=420):
        super().__init__(host)
        self.host, self.preferred_width = host, width
        self.opened = False
        self.progress = 0.0
        self._content = None
        self._previous_focus = None
        self.setObjectName("drawerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.panel = QFrame(self)
        self.panel.setObjectName("drawerPanel")
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(16)
        header = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("drawerTitle")
        header.addWidget(label, 1)
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setAccessibleName(f"关闭{title}")
        self.close_button.setToolTip(f"关闭{title} · Esc")
        self.close_button.clicked.connect(lambda: self.set_open(False))
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.content = QVBoxLayout()
        self.content.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.content, 1)
        self.opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self._finished)
        host.installEventFilter(self)
        QApplication.instance().installEventFilter(self)
        self.hide()

    def set_content(self, widget):
        if self._content is not None:
            self.content.removeWidget(self._content)
            self._content.hide()
        self._content = widget
        if widget is not None:
            self.content.addWidget(widget)
            widget.show()

    def set_open(self, opened, *, immediate=False):
        changed = self.opened != opened
        self.opened = opened
        self.animation.stop()
        if opened:
            focused = QApplication.focusWidget()
            if focused is not None and focused is not self and not self.isAncestorOf(focused):
                self._previous_focus = focused
            self.setGeometry(self.host.rect())
            self.show()
            self.raise_()
            self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)
        if immediate or not self.host.isVisible():
            self._advance(1.0 if opened else 0.0)
            self._finished()
        else:
            self.animation.setStartValue(self.progress)
            self.animation.setEndValue(1.0 if opened else 0.0)
            self.animation.start()
        if changed:
            self.openedChanged.emit(opened)

    def _advance(self, value):
        self.progress = float(value)
        self.opacity.setOpacity(self.progress)
        width = min(self.preferred_width, max(0, self.width() - 24))
        self.panel.setGeometry(
            self.width() - width + round(22 * (1 - self.progress)), 0, width, self.height()
        )

    def _finished(self):
        if not self.opened:
            self.hide()
            if self._previous_focus is not None:
                from shiboken6 import isValid

                if isValid(self._previous_focus) and self._previous_focus.isVisible():
                    self._previous_focus.setFocus(Qt.FocusReason.OtherFocusReason)
            self._previous_focus = None

    def eventFilter(self, watched, event):
        if watched is self.host and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.host.rect())
            self._advance(self.progress)
        if self.isVisible() and self.opened and event.type() == QEvent.Type.KeyPress:
            if QApplication.activePopupWidget() or QApplication.activeModalWidget():
                return False
            if isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched)):
                if event.key() == Qt.Key.Key_Escape:
                    self.set_open(False)
                    return True
                if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    backwards = event.key() == Qt.Key.Key_Backtab or bool(
                        event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    )
                    target = watched
                    while True:
                        target = (
                            target.previousInFocusChain()
                            if backwards
                            else target.nextInFocusChain()
                        )
                        if (
                            self.isAncestorOf(target)
                            and target.isVisible()
                            and target.isEnabled()
                            and target.focusPolicy() & Qt.FocusPolicy.TabFocus
                        ):
                            target.setFocus(Qt.FocusReason.TabFocusReason)
                            break
                        if target is watched:
                            break
                    return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.set_open(False)
            event.accept()
        else:
            super().mousePressEvent(event)
