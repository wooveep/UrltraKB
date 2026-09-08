"""Native conversation typography and an input-method-aware message composer."""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPlainTextEdit

from openkb.desktop.fonts import text_font
from openkb.desktop.reader import MarkdownView


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

    def show_turns(self, turns, base):
        self._turns = tuple(turns)
        background = "#30312e" if self._dark else "#f0f1ed"
        parts = []
        for question, answer in self._turns:
            question = html.escape(question).replace("\n", "<br>")
            parts.append(
                f'<table align="right" width="88%" cellspacing="0" cellpadding="14">'
                f'<tr><td bgcolor="{background}">{question}</td></tr></table>\n\n'
                "<h5>UrltraKB</h5>\n\n" + answer
            )
        super().show_markdown("\n\n".join(parts), base)

    def show_temporary(self, text):
        # MarkdownView calls this while scheduling rendering. Keep the semantic
        # turns for a palette change; explicit temporary states replace them.
        if text != "正在排版…":
            self._turns = None
        super().show_temporary(text)

    def set_presentation(self, *, dark, scale):
        turns, base = self._turns, self._base
        changed = (dark, scale) != (self._dark, self._scale)
        if changed and turns is not None:
            self._dark, self._scale = dark, scale
            self.show_turns(turns, base)
        else:
            super().set_presentation(dark=dark, scale=scale)
