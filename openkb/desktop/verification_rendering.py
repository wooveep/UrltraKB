"""Render the accepted corpus through the product's QTextDocument display."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QImage, QPainter

from openkb.desktop.reader import MarkdownView


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
