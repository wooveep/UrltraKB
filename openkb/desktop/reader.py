"""Native QTextDocument reader with background formula/diagram conversion."""

from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PySide6.QtWidgets import QTextBrowser

from openkb.desktop.fonts import MONO, SANS, text_font
from openkb.rendering.markdown import RenderedMarkdown, heading_anchor, render_markdown
from openkb.rendering.renderer import Renderer


class MarkdownView(QTextBrowser):
    rendered = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openkb-render")
        self._renderer: Renderer | None = None
        self._generation = 0
        self._futures = []
        self._source = ""
        self._base = Path.cwd()
        self._dark = False
        self._scale = 1.0
        self._closed = False
        self._has_source = False
        self._anchor = ""
        self._display_images = {}
        self._inline_images = set()
        self._source_images = {}
        self._chrome = None
        self.rendered.connect(self._apply_rendered)
        font = text_font(18)
        self.document().setDefaultFont(font)
        self.setStyleSheet("QTextBrowser { padding: 18px; border: 0; }")

    def show_markdown(
        self,
        source: str,
        base: Path,
        *,
        dark: bool | None = None,
        scale: float | None = None,
        anchor: str = "",
        preserve: bool = False,
    ) -> None:
        if self._closed:
            return
        self.show_temporary(None if preserve else "正在排版…")
        generation = self._generation
        dark = self._dark if dark is None else dark
        scale = self._scale if scale is None else scale
        self._source, self._base, self._dark, self._scale = source, base, dark, scale
        self._has_source, self._anchor = True, anchor
        self._preserve_position = preserve
        self._apply_presentation()
        cache = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
        renderer = Renderer(cache / "rendering")
        self._renderer = renderer
        self.document().setBaseUrl(QUrl.fromLocalFile(str(base) + "/"))
        future = self._pool.submit(render_markdown, source, renderer, dark=dark, scale=scale)
        self._futures.append(future)

        def completed(result):
            if not result.cancelled() and not self._closed:
                try:
                    value = result.result()
                except Exception as exc:
                    value = RenderedMarkdown(
                        f"<p>排版失败（{type(exc).__name__}）。</p><pre>{html.escape(source)}</pre>",
                        (),
                    )
                self.rendered.emit(generation, value)

        future.add_done_callback(completed)

    def _apply_rendered(self, generation: int, value: RenderedMarkdown) -> None:
        if generation != self._generation or self._closed:
            return
        previous = self.verticalScrollBar().value()
        self._display_images.clear()
        self._inline_images.clear()
        self._source_images.clear()
        self.setHtml(value.html)
        self._reader_chrome()
        for token, block in value.objects:
            cursor = self.document().find(token)
            if cursor.isNull():
                continue
            if block.error or block.image is None:
                cursor.insertText(f"[渲染失败：{block.error}]\n{block.source}\n")
                continue
            image = QTextImageFormat()
            image.setName(QUrl.fromLocalFile(block.image).toString())
            image.setWidth(block.width * self._scale)
            image.setHeight(block.height * self._scale)
            if block.display:
                self._display_images[image.name()] = image.width(), image.height()
            image.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignBaseline)
            if not block.display:
                self._inline_images.add(image.name())
                # Qt ignores baselineOffset when drawing inline images. Its
                # AlignBaseline instead uses the image format's font descent.
                # Give this image its own metrics; the surrounding text keeps
                # the document font and the image keeps its original dimensions.
                depth = block.depth * self._scale
                if depth <= 0:
                    image.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignNormal)
                else:
                    font = QFont(self.document().defaultFont())
                    font.setPointSizeF(100)
                    for _ in range(3):
                        descent = QFontMetricsF(font).descent()
                        if descent <= 0 or abs(descent - depth) < 0.05:
                            break
                        font.setPointSizeF(font.pointSizeF() * depth / descent)
                    image.setFont(font)
            image.setToolTip(block.source)
            cursor.insertImage(image)
        self._fit_source_images()
        self.document().setModified(False)
        if self._preserve_position:
            self.verticalScrollBar().setValue(previous)
        if self._anchor:
            QTimer.singleShot(0, lambda: self.scrollToAnchor(heading_anchor(self._anchor)))

    def _apply_presentation(self):
        font = self.document().defaultFont()
        font.setPixelSize(round(18 * self._scale))
        self.document().setDefaultFont(font)
        from openkb.desktop.theme import theme_colors

        colors = theme_colors(self._dark)
        self.document().setDefaultStyleSheet(f"""
            body {{ font-family: '{SANS}'; font-size: {18 * self._scale}px; }}
            p {{ line-height: 165%; margin-top: 0; margin-bottom: {16 * self._scale}px; }}
            h1, h2, h3, h4 {{ font-weight: 700;
                margin-top: {28 * self._scale}px; margin-bottom: {12 * self._scale}px; }}
            h1 {{ font-size: {30 * self._scale}px; }}
            h2 {{ font-size: {24 * self._scale}px; }}
            h3 {{ font-size: {20 * self._scale}px; }}
            h4 {{ font-size: {18 * self._scale}px; }}
            li {{ margin-bottom: {6 * self._scale}px; }}
            blockquote {{ color: {colors.muted}; background-color: {colors.subtle};
                margin: {16 * self._scale}px {20 * self._scale}px; }}
            pre, code {{ font-family: '{MONO}', '{SANS}'; font-size: {15 * self._scale}px;
                background-color: {colors.subtle}; }}
            pre {{ white-space: pre-wrap; line-height: 145%;
                margin-top: {12 * self._scale}px; margin-bottom: {20 * self._scale}px; }}
            a {{ color: {colors.accent}; text-decoration: none; }}
            table {{ border-collapse: collapse; margin-top: 12px; margin-bottom: 20px; }}
            th.markdown-cell {{ background-color: {colors.subtle}; font-weight: 700; }}
            .markdown-cell {{ padding: {10 * self._scale}px; border: 1px solid {colors.border}; }}
            hr {{ color: {colors.border}; }}
        """)
        self._reader_chrome()

    def _reader_chrome(self):
        from openkb.desktop.theme import theme_colors

        colors = theme_colors(self._dark)
        # Document margins constrain prose without increasing the widget's
        # minimum width, so a wide reader can still shrink to a narrow window.
        if self._dark != self._chrome:
            self._chrome = self._dark
            self.setStyleSheet(
                f"QTextBrowser {{ padding: 24px 18px; border: 0; border-radius: 12px; "
                f"background: {colors.surface}; color: {colors.text}; }}"
            )
        frame = self.document().rootFrame()
        format_ = frame.frameFormat()
        margin = max(4, (self.viewport().width() - round(900 * self._scale)) / 2)
        if format_.leftMargin() != margin or format_.rightMargin() != margin:
            format_.setLeftMargin(margin)
            format_.setRightMargin(margin)
            frame.setFrameFormat(format_)

    def _fit_source_images(self):
        """Fit source figures and display diagrams; retain inline formula metrics."""
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
        frame = document.rootFrame().frameFormat()
        available = max(80, self.viewport().width() - frame.leftMargin() - frame.rightMargin())
        for position, length, format_ in images:
            if format_.name() in self._inline_images:
                continue
            dimensions = self._display_images.get(format_.name())
            if dimensions is None:
                url = document.baseUrl().resolved(QUrl(format_.name()))
                if not url.isLocalFile():
                    continue
                picture = document.resource(QTextDocument.ResourceType.ImageResource, url)
                if picture is None or picture.isNull() or picture.width() <= 0:
                    continue
                if position not in self._source_images:
                    width = format_.width() or picture.width()
                    height = format_.height() or picture.height() * width / picture.width()
                    self._source_images[position] = width, height
                dimensions = self._source_images[position]
            original_width, original_height = dimensions
            if original_width <= 0:
                continue
            width = min(original_width, available)
            height = original_height * width / original_width
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
        if hasattr(self, "_chrome"):
            self._reader_chrome()
            if self._has_source:
                self._fit_source_images()

    def set_presentation(self, *, dark: bool, scale: float):
        changed = (dark, scale) != (self._dark, self._scale)
        self._dark, self._scale = dark, scale
        self._apply_presentation()
        if changed and self._has_source:
            self.show_markdown(self._source, self._base, preserve=True)

    def show_temporary(self, text: str | None) -> None:
        """Replace temporary text and revoke any older asynchronous rendering."""
        self._generation += 1
        self._has_source, self._anchor = False, ""
        if self._renderer:
            self._renderer.close()
        for future in self._futures:
            future.cancel()
        self._futures = [f for f in self._futures if not f.done()]
        if text is not None:
            self.setPlainText(text)

    def stop_rendering(self) -> None:
        self._closed = True
        if self._renderer:
            self._renderer.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def rendering_stopped(self) -> bool:
        return all(future.done() for future in self._futures)
