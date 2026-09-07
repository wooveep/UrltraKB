"""Capture actual Qt widgets from a completed batch, not a browser or image editor."""

import argparse
import json
import sys
from pathlib import Path

from bootstrap import ROOT
from PySide6.QtCore import Qt, QTimer, qVersion
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QVBoxLayout, QWidget
from run import Canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    results = json.loads(args.batch.read_text())
    app = QApplication(sys.argv[:1])
    for path in (ROOT / ".runtime" / "fonts").glob("*.otf"):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(path)
    app.setFont(QFont("Noto Sans CJK SC", 10))
    families = list(dict.fromkeys(r["family"] for r in results))
    pages = []
    for dark in (False, True):
        for family in families:
            matching = [r for r in results if r["dark"] == dark and r["family"] == family]
            for start in range(0, len(matching), 2):
                pages.append((family, dark, start, matching[start : start + 2]))
    evidence = ROOT / "evidence" / "corpus"
    evidence.mkdir(parents=True, exist_ok=True)
    report = []
    window = QWidget()
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus)
    window.resize(1480, 1000)
    outer = QVBoxLayout(window)
    current = None

    def show_page():
        nonlocal current
        if not pages:
            (evidence / "qt-capture-facts.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            app.quit()
            return
        family, dark, start, matching = pages.pop(0)
        if current:
            outer.removeWidget(current)
            current.deleteLater()
        current = QWidget()
        grid = QGridLayout(current)
        outer.addWidget(current)
        for row, result in enumerate(matching):
            for column, mode in enumerate(("SVG", "PNG")):
                item = QWidget()
                layout = QVBoxLayout(item)
                title = QLabel(f"{result['id']} · {'深色' if dark else '浅色'} · {mode}")
                layout.addWidget(title)
                expected = QLabel(result["expected"])
                expected.setWordWrap(True)
                layout.addWidget(expected)
                canvas = Canvas(mode)
                canvas.set_result(result)
                layout.addWidget(canvas, 1)
                grid.addWidget(item, row, column)
                report.append(
                    {
                        "id": result["id"],
                        "dark": dark,
                        "mode": mode,
                        "qt": qVersion(),
                        "platform": app.platformName(),
                        "svg_valid": canvas.svg.isValid() if result["ok"] else None,
                        "semantic_review": "pending",
                    }
                )
        window.setWindowTitle(f"原生 Qt 语料核对 · {family}")
        window.show()

        def capture():
            path = evidence / f"{matching[0]['id']}-{'dark' if dark else 'light'}.png"
            window.grab().save(str(path))
            print(path.name, flush=True)
            QTimer.singleShot(20, show_page)

        QTimer.singleShot(150, capture)

    show_page()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
