"""Rounded native message cards with selectable text and independent Markdown readers."""

import math
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from openkb.agent.answer_text import visible_answer
from openkb.desktop.conversation_view import ConversationView as MessageReader
from openkb.desktop.fonts import text_font
from openkb.desktop.theme import theme_colors


class _AnswerReader(MessageReader):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMinimumWidth(0)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(self.fit_height)
        self.textChanged.connect(owner.textChanged)
        self.anchorClicked.connect(owner.anchorClicked)
        self._target = self._shown = ""
        self._live = False
        self._typing = QTimer(self)
        self._typing.setInterval(40)
        self._typing.timeout.connect(self._type_next)

    def stream_to(self, text, *, live):
        self.show_temporary(None)
        self._live = live
        if not text.startswith(self._shown):
            self._shown = ""
        self._target = text
        if not self._typing.isActive():
            self._typing.start()

    def _type_next(self):
        from openkb.rendering.markdown import render_markdown

        following = self.owner.following()
        remaining = len(self._target) - len(self._shown)
        # Smooth small deltas, catch up large bursts without a long typing tail.
        step = max(2, math.ceil(remaining / 8))
        self._shown = self._target[: len(self._shown) + step]
        self._apply_presentation()
        self.document().setBaseUrl(QUrl.fromLocalFile(str(self.owner._base) + "/"))
        self.setHtml(render_markdown(self._shown, None, dark=self._dark).html)
        self.fit_height()
        if following:
            self.owner.follow_end()
        if self._shown == self._target:
            self._typing.stop()
            if not self._live:
                self.show_markdown(self._target, self.owner._base, preserve=True)

    def stop_rendering(self):
        self._typing.stop()
        super().stop_rendering()

    def fit_height(self, *_):
        height = max(40, math.ceil(self.document().size().height()) + 16)
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def _apply_rendered(self, generation, value):
        if self._closed or generation != self._generation:
            return
        following = self.owner.following()
        super()._apply_rendered(generation, value)
        self.setStyleSheet("QTextBrowser { background: transparent; border: 0; padding: 4px; }")
        self.fit_height()
        if following:
            self.owner.follow_end()

    def set_presentation(self, *, dark, scale):
        self.owner.style_cards(dark, scale)
        super().set_presentation(dark=dark, scale=scale)
        if self._live or self._typing.isActive():
            self._type_next()


class ConversationView(QScrollArea):
    """Preserve chat's small reader interface while each message owns its rounded frame."""

    anchorClicked = Signal(QUrl)
    textChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("conversationTranscript")
        self._dark, self._scale = False, 1.0
        self._base = Path.cwd()
        self._signature = None
        self._identity = None
        self._entries = []
        self._retired = []
        self._closed = False
        self.canvas = QWidget()
        self.body = QVBoxLayout(self.canvas)
        self.body.setContentsMargins(4, 8, 4, 16)
        self.body.setSpacing(18)
        self.body.addStretch()
        self.setWidget(self.canvas)
        self.setStyleSheet(
            "QScrollArea#conversationTranscript { border: 0; background: transparent; }"
        )

    def following(self):
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() < 48

    def follow_end(self):
        QTimer.singleShot(
            0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        )

    def _clear(self, start=0):
        for _, _, row, _, reader in self._entries[start:]:
            if reader:
                reader.stop_rendering()
                self._retired.append(reader)
            self.body.removeWidget(row)
            row.hide()
            if not reader:
                row.deleteLater()
        del self._entries[start:]
        # Closed readers stay owned until their asynchronous rendering has settled.
        for reader in list(self._retired):
            if reader.rendering_stopped():
                reader.parentWidget().parentWidget().deleteLater()
                self._retired.remove(reader)

    def show_turns(self, turns, base, *, pending=None, identity=None):
        turns = tuple(turns)
        signature = (turns, pending, Path(base), identity)
        if self._closed or signature == self._signature:
            return
        finishing = bool(
            self._signature
            and self._signature[1]
            and not pending
            and identity == self._identity
            and turns
            and self._signature[1][0] == turns[-1][0]
        )
        wanted = []
        rows = (*turns, *((pending,) if pending else ()))
        for index, (question, answer) in enumerate(rows):
            wanted.append(("question", question))
            if answer:
                live = bool(pending) and index == len(rows) - 1
                wanted.append(("answer", visible_answer(answer, streaming=live)))
        following = self.following()
        if Path(base) != self._base or identity != self._identity:
            self._clear()
        self._identity = identity
        self._signature, self._base = signature, Path(base)
        for index, (role, text) in enumerate(wanted):
            if index >= len(self._entries):
                break
            old_role, old_text, row, label, reader = self._entries[index]
            if role == old_role and (text == old_text or role == "answer"):
                if reader and (text != old_text or reader._live):
                    reader.stream_to(text, live=bool(pending) and index == len(wanted) - 1)
                    self._entries[index] = (role, text, row, label, reader)
            else:
                self._clear(index)
                break
        if len(self._entries) > len(wanted):
            self._clear(len(wanted))
        for role, text in wanted[len(self._entries) :]:
            last = len(self._entries) == len(wanted) - 1
            self._append(role, text, live=bool(pending) and last, animate=finishing and last)
        self.style_cards(self._dark, self._scale)
        self.textChanged.emit()
        if following:
            self.follow_end()

    def _append(self, role, text, *, live=False, animate=False):
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("userMessage" if role == "question" else "answerMessage")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        label = QLabel(text if role == "question" else "UrltraKB")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        reader = None
        if role == "question":
            outer.addStretch(1)
            outer.addWidget(card, 4)
        else:
            reader = _AnswerReader(self)
            reader.set_presentation(dark=self._dark, scale=self._scale)
            if live or animate:
                reader.stream_to(text, live=live)
            else:
                reader.show_markdown(text, self._base)
            layout.addWidget(reader)
            outer.addWidget(card, 1)
        self.body.insertWidget(self.body.count() - 1, row)
        self._entries.append((role, text, row, label, reader))

    def style_cards(self, dark, scale):
        self._dark, self._scale = dark, scale
        colors = theme_colors(dark)
        self.canvas.setStyleSheet(
            f"QFrame#userMessage {{ background: {colors.selection}; "
            f"border: 1px solid {colors.border}; border-radius: 18px; }}"
            f"QFrame#answerMessage {{ background: {colors.surface}; "
            f"border: 1px solid {colors.border}; border-radius: 18px; }}"
        )
        for role, _, _, label, _ in self._entries:
            label.setFont(text_font(round((16 if role == "question" else 13) * scale)))
            label.setStyleSheet(
                f"color: {colors.text if role == 'question' else colors.muted}; "
                "background: transparent;"
            )

    def set_presentation(self, *, dark, scale):
        self.style_cards(dark, scale)
        for *_, reader in self._entries:
            if reader:
                reader.set_presentation(dark=dark, scale=scale)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "canvas"):
            self.style_cards(
                self.palette().color(QPalette.ColorRole.Window).lightness() < 128, self._scale
            )

    def toPlainText(self):
        return "\n\n".join(
            reader.toPlainText() if reader else text for _, text, _, _, reader in self._entries
        )

    def show_temporary(self, text):
        if text is None:
            return
        self._clear()
        self._signature = None
        if text:
            self._append("question", text)
        self.style_cards(self._dark, self._scale)
        self.textChanged.emit()

    def show_markdown(self, source, base, **kwargs):
        self._clear()
        self._signature, self._base = None, Path(base)
        self._append("answer", source)
        self.set_presentation(
            dark=kwargs.get("dark", self._dark), scale=kwargs.get("scale", self._scale)
        )

    def stop_rendering(self):
        self._closed = True
        for reader in [entry[-1] for entry in self._entries if entry[-1]] + self._retired:
            reader.stop_rendering()

    def rendering_stopped(self):
        return all(
            reader.rendering_stopped()
            for reader in [entry[-1] for entry in self._entries if entry[-1]] + self._retired
        )
