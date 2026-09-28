"""Read retained original-format artifacts without exposing compilation internals."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

from openkb.desktop.reader import MarkdownView


class SourceReader(QDialog):
    def __init__(self, window, kb, source):
        super().__init__(window)
        self.setWindowTitle(f"原文 · {source['name']}")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 720)
        layout = QVBoxLayout(self)
        hint = QLabel("导入时保留的原文 · 摘要、概念和实体请在知识页阅读")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.reader = MarkdownView()

        def follow(url):
            if not url.scheme() and url.fragment():
                from openkb.rendering.markdown import heading_anchor

                self.reader.scrollToAnchor(heading_anchor(url.fragment()))
            else:
                window._follow_link(url)

        self.reader.anchorClicked.connect(follow)
        layout.addWidget(self.reader, 1)
        self.reader.show_markdown(
            source["content"],
            kb / "wiki/sources",
            dark=window.appearance.dark,
            scale=window.zoom.currentData(),
        )

    def closeEvent(self, event):
        self.reader.stop_rendering()
        super().closeEvent(event)


def show_source(window, kb, source):
    dialog = SourceReader(window, kb, source)
    dialog.show()
    return dialog
