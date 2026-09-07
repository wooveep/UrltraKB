"""THROWAWAY native rendering evidence viewer. Launch with .venv/bin/python run.py."""

import argparse
import json
import sys
from pathlib import Path

from bootstrap import ROOT
from PySide6.QtCore import QProcess, QRectF, Qt, QTimer, qVersion
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from samples import SAMPLES


class Canvas(QWidget):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.result = None
        self.svg = QSvgRenderer(self)
        self.png = QImage()
        self.fit = True
        self.zoom = 1.0
        self.setMinimumSize(280, 220)

    def set_result(self, result, fit=True, zoom=1.0):
        self.result, self.fit, self.zoom = result, fit, zoom
        if result.get("ok"):
            self.svg.load(result["svg_path"])
            self.png.load(result["png_path"])
            if not fit:
                size = self.svg.defaultSize()
                self.setMinimumSize(int(size.width() * zoom) + 40, int(size.height() * zoom) + 40)
            else:
                self.setMinimumSize(280, 220)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = bool(self.result and self.result.get("dark"))
        painter.fillRect(self.rect(), QColor("#172131" if dark else "#ffffff"))
        if not self.result or not self.result.get("ok"):
            painter.setPen(QColor("#e9b47e" if dark else "#834818"))
            message = self.result.get("error", "选择样本后运行") if self.result else "准备原型…"
            painter.drawText(
                self.rect().adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                message,
            )
            return
        size = self.svg.defaultSize()
        if size.isEmpty():
            return
        factor = (
            min((self.width() - 40) / size.width(), (self.height() - 40) / size.height())
            if self.fit
            else self.zoom
        )
        if self.fit:
            factor = min(factor, 2.0)
        width, height = size.width() * factor, size.height() * factor
        rect = QRectF(
            max(20, (self.width() - width) / 2),
            max(20, (self.height() - height) / 2),
            width,
            height,
        )
        if self.mode == "SVG":
            self.svg.render(painter, rect)
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, self.png)


class ParagraphPreview(QWidget):
    """Direct Qt baseline placement probe; does not claim to be a complete Markdown editor."""

    def __init__(self):
        super().__init__()
        self.result = None
        self.svg = QSvgRenderer(self)
        self.png = QImage()
        self.setMinimumHeight(260)

    def set_result(self, result):
        self.result = result
        if result.get("ok"):
            self.svg.load(result["svg_path"])
            self.png.load(result["png_path"])
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        dark = bool(self.result and self.result.get("dark"))
        p.fillRect(self.rect(), QColor("#172131" if dark else "white"))
        p.setPen(QColor("#e8edf5" if dark else "#1c2738"))
        font = QFont("Noto Sans CJK SC")
        font.setPixelSize(20)
        p.setFont(font)
        if not self.result or not self.result.get("ok") or self.result.get("kind") != "math":
            p.drawText(35, 65, "请选择公式样本查看 Qt 正文与基线。")
            return
        p.drawText(35, 50, "行内混排探针：蓝线标记正文基线，公式按深度放置。")
        baseline = 165.0
        p.setPen(QPen(QColor("#4285b5"), 1, Qt.PenStyle.DashLine))
        p.drawLine(35, int(baseline), self.width() - 35, int(baseline))
        p.setPen(QColor("#e8edf5" if dark else "#1c2738"))
        prefix, suffix = "正文中的公式 ", " 之后继续中文说明。"
        p.drawText(35, baseline, prefix)
        x = 35 + QFontMetricsF(font).horizontalAdvance(prefix)
        size = self.svg.defaultSize()
        depth = self.result.get("depth_px", 0)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(
            QRectF(x, baseline - size.height() + depth, size.width(), size.height()), self.png
        )
        p.drawText(x + size.width(), baseline, suffix)
        p.drawText(
            35,
            230,
            f"SVG {size.width()} × {size.height()} px；声明深度 {depth:g} px。此视图需人工检查。",
        )


class Window(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args, self.current, self.result = args, None, None
        self.process = None
        self.setWindowTitle("OpenKB · 原生渲染原型（可丢弃）")
        self.resize(1480, 900)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        heading = QLabel("原生内容渲染 · SVG / PNG 对照")
        heading.setStyleSheet("font-size:22px;font-weight:600;padding:8px")
        layout.addWidget(heading)
        toolbar = QHBoxLayout()
        self.theme = QCheckBox("深色主题")
        self.theme.setChecked(args.dark)
        self.scale = QComboBox()
        self.scale.addItems(["适应窗口", "100%", "150%", "200%", "400%"])
        self.scale.setCurrentIndex(
            {1: 1, 1.5: 2, 2: 3, 4: 4}.get(args.scale, 0) if args.scale else 0
        )
        self.run_button = QPushButton("重新渲染")
        self.copy_button = QPushButton("复制原始 Markdown")
        self.capture_button = QPushButton("保存窗口截图")
        for widget in (
            self.theme,
            self.scale,
            self.run_button,
            self.copy_button,
            self.capture_button,
        ):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        split = QSplitter()
        layout.addWidget(split, 1)
        self.listing = QListWidget()
        self.listing.setMinimumWidth(220)
        for sample in SAMPLES:
            self.listing.addItem(f"{sample['family']} · {sample['title']}")
        split.addWidget(self.listing)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        split.addWidget(right)
        split.setSizes([250, 1150])
        self.title = QLabel()
        self.title.setWordWrap(True)
        right_layout.addWidget(self.title)
        self.expected = QLabel()
        self.expected.setWordWrap(True)
        right_layout.addWidget(self.expected)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)
        pair = QWidget()
        pair_layout = QHBoxLayout(pair)
        self.canvases = []
        for mode in ("SVG", "PNG"):
            column = QWidget()
            col_layout = QVBoxLayout(column)
            col_layout.addWidget(
                QLabel("Qt SVG 直接绘制" if mode == "SVG" else "resvg + 随包字体 → PNG → Qt")
            )
            canvas = Canvas(mode)
            self.canvases.append(canvas)
            scroll = QScrollArea()
            scroll.setWidget(canvas)
            scroll.setWidgetResizable(True)
            col_layout.addWidget(scroll, 1)
            pair_layout.addWidget(column, 1)
        self.tabs.addTab(pair, "最终 Qt 对照")
        self.paragraph = ParagraphPreview()
        self.tabs.addTab(self.paragraph, "公式正文与基线")
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.tabs.addTab(self.diagnostics, "运行事实与诊断")
        right_layout.addWidget(QLabel("原始 Markdown（可编辑；不修改知识库）"))
        self.source = QPlainTextEdit()
        self.source.setMaximumHeight(180)
        right_layout.addWidget(self.source)
        self.status = QLabel("尚未进行视觉验收")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.run_button.clicked.connect(self.render_current)
        self.theme.toggled.connect(self.render_current)
        self.scale.currentIndexChanged.connect(self.render_current)
        self.copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.source.toPlainText())
        )
        self.capture_button.clicked.connect(self.capture)
        self.listing.currentRowChanged.connect(self.select)
        self.listing.setCurrentRow(next(i for i, s in enumerate(SAMPLES) if s["id"] == args.sample))

    def select(self, index):
        self.current = dict(SAMPLES[index])
        self.source.setPlainText(self.current["markdown"])
        self.title.setText(self.current["title"])
        self.expected.setText("人工核对：" + self.current["expected"])
        self.render_current()

    def render_current(self, *unused):
        if not self.current or self.process:
            return
        temporary = ROOT / "artifacts" / "edited-source.md"
        temporary.parent.mkdir(exist_ok=True)
        temporary.write_text(self.source.toPlainText(), encoding="utf-8")
        self.status.setText("本地工作进程渲染中…")
        self.run_button.setEnabled(False)
        self.listing.setEnabled(False)
        self.scale.setEnabled(False)
        self.theme.setEnabled(False)
        factor = [2, 1, 1.5, 2, 4][self.scale.currentIndex()] * self.devicePixelRatioF()
        arguments = [
            str(ROOT / "render.py"),
            "--sample",
            self.current["id"],
            "--source-file",
            str(temporary),
            "--scale",
            str(factor),
        ]
        if self.theme.isChecked():
            arguments.append("--dark")
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.finished.connect(self.finished)
        self.process.start(sys.executable, arguments)

    def finished(self, code, status):
        raw = bytes(self.process.readAllStandardOutput()).decode("utf-8")
        stderr = bytes(self.process.readAllStandardError()).decode("utf-8")
        self.process.deleteLater()
        self.process = None
        try:
            result = json.loads(raw)
        except ValueError:
            result = {
                "ok": False,
                "error": stderr or raw or f"工作进程退出 {code}",
                "dark": self.theme.isChecked(),
            }
        self.result = result
        self.run_button.setEnabled(True)
        self.listing.setEnabled(True)
        self.scale.setEnabled(True)
        self.theme.setEnabled(True)
        for canvas in self.canvases:
            canvas.set_result(
                result, self.scale.currentIndex() == 0, [1, 1, 1.5, 2, 4][self.scale.currentIndex()]
            )
        self.paragraph.set_result(result)
        self.diagnostics.setPlainText(
            json.dumps(
                {
                    **result,
                    "qt": qVersion(),
                    "qt_platform": QApplication.platformName(),
                    "qt_svg_valid": self.canvases[0].svg.isValid() if result.get("ok") else False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if result.get("ok"):
            self.status.setText(
                f"技术生成成功 · {result['elapsed_ms']} ms · 随包字体 {result['font_faces']} 个 · "
                f"语义与布局待人工验收 · Qt {qVersion()} / {QApplication.platformName()}"
            )
        else:
            self.status.setText("已明确报告错误，原文保留：" + result.get("error", "未知错误"))
        if self.args.paragraph:
            self.tabs.setCurrentIndex(1)
        if self.args.capture:
            QTimer.singleShot(350, lambda: self.capture(self.args.capture, True))

    def capture(self, path=None, quit_after=False):
        if not isinstance(path, str):
            path = str(ROOT / "artifacts" / f"window-{self.current['id']}.png")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.grab().save(path)
        print(f"CAPTURE {path}", flush=True)
        if quit_after:
            QApplication.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="flow-cn")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--scale", type=float)
    parser.add_argument("--paragraph", action="store_true")
    parser.add_argument("--capture")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    for path in sorted((ROOT / ".runtime" / "fonts").glob("*.otf")):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f"Qt could not load {path.name}")
    font = QFont("Noto Sans CJK SC")
    font.setPointSize(10)
    app.setFont(font)
    window = Window(args)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
