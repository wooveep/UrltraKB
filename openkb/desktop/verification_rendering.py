"""Render the accepted corpus through the product's QTextDocument display."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QImage, QPainter

from openkb.desktop.reader import MarkdownView


def _colored_top(image: QImage, color: str) -> int:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = bytes(rgba.constBits())
    ink = bytes.fromhex(color.removeprefix("#")) + b"\xff"
    offset = pixels.find(ink)
    while offset >= 0 and offset % 4:
        offset = pixels.find(ink, offset + 1)
    assert offset >= 0, "Formula ink is missing from the final Qt image"
    return offset // rgba.bytesPerLine()


def _inline_baseline(view, block, image: QImage, dark: bool, scale: float) -> float:
    # MathJax's ink differs from the native prose color. Locate the actual
    # raster in the painted document, then compare its declared mathematical
    # baseline with the surrounding QTextLine; format properties alone are not
    # evidence that Qt actually positioned an image correctly.
    ink = "#e8edf5" if dark else "#1c2738"
    raster = QImage(block.image)
    top = _colored_top(image, ink) - _colored_top(raster, ink)
    cursor = view.document().find("\ufffc")
    text_block = cursor.block()
    layout = text_block.layout()
    line = layout.lineForTextPosition(cursor.selectionStart() - text_block.position())
    baseline = layout.position().y() + line.y() + line.ascent()
    rendered_baseline = top + raster.height() - block.depth * scale
    error = rendered_baseline - baseline
    assert abs(error) <= 1, f"Inline formula baseline differs from prose by {error:.2f}px"
    return error


def verify_corpus(corpus: Path, output: Path, wait_until) -> None:
    view = MarkdownView()
    view.resize(1280, 900)
    view.show()
    samples = json.loads(corpus.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    rendered = []

    def received(generation, value):
        if generation == view._generation:
            rendered.append(value)

    view.rendered.connect(received)
    try:
        for scale in (1, 1.5, 2, 4):
            for dark in (False, True):
                for sample in samples:
                    rendered.clear()
                    source = sample["markdown"]
                    prefix = f"{sample['id']}-{'dark' if dark else 'light'}-{scale:g}"
                    view.show_markdown(source, output, dark=dark, scale=scale)
                    wait_until(lambda: bool(rendered))
                    value = rendered[-1]
                    assert len(value.objects) == 1, (sample["id"], value.html)
                    block = value.objects[0][1]
                    if "display" in sample:
                        assert block.display == sample["display"]
                    failed = bool(block.error)
                    assert failed == bool(sample.get("expected_error")), (sample["id"], block)
                    if failed:
                        assert block.source in view.toPlainText()
                    else:
                        assert Path(block.image).is_file()
                        assert "\ufffc" in view.toPlainText()
                        assert "渲染失败" not in view.toPlainText()
                    # Paint the actual Qt document at its full logical extent,
                    # not a separately rendered SVG masquerading as UI evidence.
                    size = view.document().size()
                    width, height = math.ceil(size.width()), math.ceil(size.height())
                    assert 0 < width <= 16384 and 0 < height <= 16384
                    image = QImage(width, height, QImage.Format.Format_ARGB32)
                    image.fill(QColor("#151922" if dark else "#ffffff"))
                    painter = QPainter(image)
                    context = QAbstractTextDocumentLayout.PaintContext()
                    context.palette = view.palette()
                    view.document().documentLayout().draw(painter, context)
                    painter.end()
                    baseline_error = None
                    if sample.get("display") is False and not failed:
                        assert "行内 x" in view.toPlainText() and "正文基线" in view.toPlainText()
                        baseline_error = _inline_baseline(view, block, image, dark, scale)
                    assert image.save(str(output / f"{prefix}.png"))
                    (output / f"{prefix}.md").write_text(source, encoding="utf-8")
                    rows.append(
                        {
                            "id": sample["id"],
                            "dark": dark,
                            "scale": scale,
                            "expected_error": failed,
                            "error": block.error,
                            "image": f"{prefix}.png",
                            "expectation": sample["expected"],
                            "baseline_error_px": baseline_error,
                        }
                    )
            print(f"Native rendering: scale {scale:g}, {len(rows)} cases checked", flush=True)
    finally:
        view.rendered.disconnect(received)
        view.stop_rendering()
        wait_until(view.rendering_stopped)
        view.close()
        (output / "results.json").write_text(
            json.dumps({"runs": rows, "visual_verdict": "pending"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
