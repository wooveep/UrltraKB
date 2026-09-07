"""Native QTextDocument reader with background formula/diagram conversion."""

from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QTextCharFormat, QTextImageFormat
from PySide6.QtWidgets import QTextBrowser

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
        self.rendered.connect(self._apply_rendered)
        font = QFont("Noto Sans CJK SC")
        font.setPixelSize(20)
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
    ) -> None:
        if self._closed:
            return
        self.show_temporary("正在排版…")
        generation = self._generation
        dark = self._dark if dark is None else dark
        scale = self._scale if scale is None else scale
        self._source, self._base, self._dark, self._scale = source, base, dark, scale
        self._has_source, self._anchor = True, anchor
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
        self.setHtml(value.html)
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
            image.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignBaseline)
            if not block.display:
                image.setBaselineOffset(-block.depth / 20 * 100)
            image.setToolTip(block.source)
            cursor.insertImage(image)
        self.document().setModified(False)
        if self._anchor:
            QTimer.singleShot(0, lambda: self.scrollToAnchor(heading_anchor(self._anchor)))

    def _apply_presentation(self):
        font = self.document().defaultFont()
        font.setPixelSize(round(20 * self._scale))
        self.document().setDefaultFont(font)
        background, foreground = ("#151922", "#e7eaf0") if self._dark else ("#ffffff", "#17202c")
        self.setStyleSheet(
            f"QTextBrowser {{ padding: 18px; border: 0; background: {background}; "
            f"color: {foreground}; }}"
        )

    def set_presentation(self, *, dark: bool, scale: float):
        self._dark, self._scale = dark, scale
        self._apply_presentation()
        if self._has_source:
            self.show_markdown(self._source, self._base)

    def show_temporary(self, text: str) -> None:
        """Replace temporary text and revoke any older asynchronous rendering."""
        self._generation += 1
        self._has_source, self._anchor = False, ""
        if self._renderer:
            self._renderer.close()
        for future in self._futures:
            future.cancel()
        self._futures = [f for f in self._futures if not f.done()]
        self.setPlainText(text)

    def stop_rendering(self) -> None:
        self._closed = True
        if self._renderer:
            self._renderer.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def rendering_stopped(self) -> bool:
        return all(future.done() for future in self._futures)
