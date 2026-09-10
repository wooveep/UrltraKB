"""Native conversation typography and an input-method-aware message composer."""

import html

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit

from openkb.agent.answer_text import visible_answer
from openkb.desktop.fonts import text_font
from openkb.desktop.reader import MarkdownView
from openkb.desktop.theme import theme_colors


class QuestionEdit(QPlainTextEdit):
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self._composing = False
        self.setObjectName("question")
        self.setAccessibleName("向知识库提问")
        self.setFont(text_font(16))
        self.setPlaceholderText("提问、梳理思路，或继续探索…")
        self.setTabChangesFocus(True)
        self.setFixedHeight(64)
        self.document().documentLayout().documentSizeChanged.connect(self._resize_input)

    def _resize_input(self, size):
        block = self.document().begin()
        lines = 0
        while block.isValid():
            lines += max(1, block.layout().lineCount())
            block = block.next()
        self.setFixedHeight(max(64, min(144, lines * self.fontMetrics().lineSpacing() + 20)))

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and not self._composing
        ):
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ConversationView(MarkdownView):
    def __init__(self):
        super().__init__()
        self._turns = None
        self._pending = None

    def show_turns(self, turns, base, *, pending=None):
        self._turns = tuple(turns)
        self._pending = pending
        background = theme_colors(self._dark).selection
        parts = []
        for question, answer in (*self._turns, *((pending,) if pending else ())):
            question = html.escape(question).replace("\n", "<br>")
            parts.append(
                '<table width="100%" cellspacing="0" cellpadding="14">'
                f'<tr><td width="22%"></td><td bgcolor="{background}">{question}</td></tr>'
                '</table>\n\n<p style="font-size: 13px; margin-top: 24px;">UrltraKB</p>\n\n'
                + visible_answer(answer)
                + '\n\n<p style="margin-bottom: 28px;"></p>'
            )
        if not parts:
            self.show_temporary("")
        else:
            super().show_markdown("\n\n".join(parts), base, preserve=True)

    def _apply_rendered(self, generation, value):
        bar = self.verticalScrollBar()
        previous = bar.value()
        following = bar.maximum() - previous < 40
        super()._apply_rendered(generation, value)
        if generation == self._generation:
            self._fit_source_images()
            bar.setValue(bar.maximum() if following else previous)

    def _fit_source_images(self):
        document = self.document()
        images = []
        block = document.begin()
        while block.isValid():
            fragment = block.begin()
            while not fragment.atEnd():
                value = fragment.fragment()
                if value.charFormat().isImageFormat():
                    images.append(
                        (value.position(), value.length(), value.charFormat().toImageFormat())
                    )
                fragment += 1
            block = block.next()
        for position, length, format_ in images:
            url = document.baseUrl().resolved(QUrl(format_.name()))
            if not url.isLocalFile():
                continue
            from pathlib import Path

            if not Path(url.toLocalFile()).resolve().is_relative_to(self._base.resolve()):
                continue  # Formula/diagram rendering has its own size and baseline.
            picture = document.resource(QTextDocument.ResourceType.ImageResource, url)
            if picture is None or picture.isNull() or picture.width() <= 0:
                continue
            width = min(picture.width(), max(120, self.viewport().width() - 48))
            height = picture.height() * width / picture.width()
            if format_.width() == width and format_.height() == height:
                continue
            format_.setWidth(width)
            format_.setHeight(height)
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(format_)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._has_source:
            self._fit_source_images()

    def show_temporary(self, text):
        # MarkdownView calls this while scheduling rendering. Keep the semantic
        # turns for a palette change; explicit temporary states replace them.
        if text not in (None, "正在排版…"):
            self._turns = None
            self._pending = None
        super().show_temporary(text)

    def set_presentation(self, *, dark, scale):
        turns, base = self._turns, self._base
        changed = (dark, scale) != (self._dark, self._scale)
        if changed and turns is not None:
            self._dark, self._scale = dark, scale
            self.show_turns(turns, base, pending=self._pending)
        else:
            super().set_presentation(dark=dark, scale=scale)
