"""Native, throwaway onedir probe; no product navigation or persistence outside scratch output."""

import argparse
import json
import multiprocessing
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, qVersion
from PySide6.QtGui import QFont, QFontDatabase, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from openkb.portable_prototype.paths import assets
from openkb.portable_prototype.worker import execute


class Window(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.start = time.monotonic()
        self.closing = False
        self.ctx = multiprocessing.get_context("spawn")
        self.jobs = []
        self.events = []
        self.results = {}
        self.phase = 0
        self.phases = [
            ["core-A", "core-B"],
            ["renderers", "transport"],
            ["crash"],
            ["recover"],
            ["stop"],
        ]
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.summary = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "os_release": platform.freedesktop_os_release() if sys.platform == "linux" else {},
            "qt": qVersion(),
            "qt_platform": QApplication.platformName(),
            "pid": os.getpid(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "assets": str(assets()),
            "cwd": os.getcwd(),
            "home": str(Path.home()),
            "profile_mode": "system" if args.use_system_profile else "scratch",
            "external_tools": {
                n: shutil.which(n) for n in ("python", "python3", "node", "cargo", "gcc")
            },
            "tray_available": QSystemTrayIcon.isSystemTrayAvailable(),
            "force_no_tray": args.no_tray,
        }
        self.setWindowTitle("OpenKB · 免安装运行探针（可丢弃）")
        self.resize(1280, 880)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.label = QLabel("准备冻结工作进程…")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        info = QLabel(
            f"冻结：{self.summary['frozen']} · Qt {qVersion()} / {self.summary['qt_platform']}"
            f" · 程序：{sys.executable}"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        close_button = QPushButton("关闭窗口（验证托盘/回退）")
        close_button.clicked.connect(self.close)
        stop_button = QPushButton("请求安全停止")
        stop_button.clicked.connect(self.stop)
        exit_button = QPushButton("真正退出")
        exit_button.clicked.connect(self.request_exit)
        for b in (close_button, stop_button, exit_button):
            buttons.addWidget(b)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        self.listing = QListWidget()
        splitter.addWidget(self.listing)
        self.preview = QLabel("随包渲染结果将在这里显示")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(500, 350)
        splitter.addWidget(self.preview)
        splitter.setSizes([280, 920])
        self.images = []
        self.listing.currentRowChanged.connect(self.select_image)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(250)
        layout.addWidget(self.log)
        self.log.appendPlainText(json.dumps(self.summary, ensure_ascii=False, indent=2))
        self.tray = None
        if self.summary["tray_available"] and not args.no_tray:
            self.tray = QSystemTrayIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self
            )
            menu = QMenu()
            show = menu.addAction("显示探针")
            show.triggered.connect(self.showNormal)
            leave = menu.addAction("退出探针")
            leave.triggered.connect(self.request_exit)
            self.tray.setContextMenu(menu)
            self.tray.setToolTip("OpenKB 便携原型")
            self.tray.show()
            self.tray.activated.connect(lambda reason: self.showNormal())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(40)
        QTimer.singleShot(150, self.start_phase)
        QTimer.singleShot(350, self.exercise_close)

    def exercise_close(self):
        running = [job["name"] for job in self.jobs]
        self.close()
        self.summary["close_during_work"] = {
            "running": running,
            "visible_after_close": self.isVisible(),
            "tray_created": self.tray is not None,
        }
        QTimer.singleShot(1500, self.showNormal)

    def start_phase(self):
        if self.closing or self.phase >= len(self.phases):
            self.finish()
            return
        for name in self.phases[self.phase]:
            receiver, sender = self.ctx.Pipe(duplex=False)
            stop = self.ctx.Event()
            folder = self.output / ("recovery" if name in ("crash", "recover") else name)
            process = self.ctx.Process(
                target=execute, args=(name, str(folder), sender, stop), name=name
            )
            process.start()
            sender.close()
            self.jobs.append({"name": name, "process": process, "pipe": receiver, "stop": stop})
        self.label.setText("运行阶段：" + "、".join(self.phases[self.phase]))
        self.phase += 1

    def tick(self):
        completed = []
        for job in self.jobs:
            try:
                while job["pipe"].poll():
                    event = job["pipe"].recv()
                    self.events.append(event)
                    kind = event["event"]
                    data = event["data"]
                    name = job["name"]
                    if kind == "result":
                        self.results[name] = data
                    if kind == "boundary":
                        job["stop"].set()
                    if kind == "render" and data["ok"]:
                        self.images.append(data["png"])
                        self.listing.addItem(data["id"] + (" · 深色" if data["dark"] else ""))
                        if len(self.images) == 1:
                            self.listing.setCurrentRow(0)
                    if kind != "render":
                        self.log.appendPlainText(json.dumps(event, ensure_ascii=False))
            except EOFError:
                pass
            if job["process"].exitcode is not None:
                job["process"].join()
                code = job["process"].exitcode
                self.events.append(
                    {
                        "job": job["name"],
                        "event": "reaped",
                        "pid": job["process"].pid,
                        "exitcode": code,
                    }
                )
                if job["name"] == "crash":
                    self.results["crash"] = {"ok": code == 23, "intentional_exitcode": code}
                elif job["name"] not in self.results:
                    self.results[job["name"]] = {
                        "ok": False,
                        "exitcode": code,
                        "missing_result": True,
                    }
                job["pipe"].close()
                completed.append(job)
        for job in completed:
            self.jobs.remove(job)
        if completed and not self.jobs:
            self.start_phase()

    def finish(self):
        self.timer.stop()
        self.summary.update(
            results=self.results,
            events=self.events,
            elapsed_seconds=round(time.monotonic() - self.start, 2),
            active_workers=len(multiprocessing.active_children()),
            all_expected=all(r.get("ok") for r in self.results.values()) and len(self.results) == 7,
        )
        if self.tray is None:
            self.close()
            self.summary["no_tray_close_kept_visible"] = self.isVisible()
        (self.output / "summary.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.label.setText(
            ("探针符合预期" if self.summary["all_expected"] else "存在失败，查看下方记录")
            + "；这不是模型质量或正式产品验收。"
        )
        self.log.appendPlainText(
            json.dumps(
                {
                    "all_expected": self.summary["all_expected"],
                    "active_workers": self.summary["active_workers"],
                },
                ensure_ascii=False,
            )
        )
        QTimer.singleShot(250, self.capture)

    def capture(self):
        self.grab().save(str(self.output / "window.png"))
        if self.args.auto_exit or self.closing:
            self.closing = True
            QApplication.quit()

    def select_image(self, index):
        if index >= 0:
            pixmap = QPixmap(self.images[index])
            self.preview.setPixmap(
                pixmap.scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def stop(self):
        for job in self.jobs:
            job["stop"].set()
        self.label.setText("已请求停止；等待当前安全边界。")

    def request_exit(self):
        self.closing = True
        self.stop()
        if not self.jobs:
            QApplication.quit()

    def closeEvent(self, event):
        if self.closing and not self.jobs:
            event.accept()
            return
        event.ignore()
        if self.tray:
            self.hide()
        else:
            self.label.setText("当前无可用托盘，窗口保持可见。真正退出请点“真正退出”。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--auto-exit", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument(
        "--use-system-profile",
        action="store_true",
        help="Only for a disposable OS user/container; registers test KBs there",
    )
    args = parser.parse_args()
    if args.output is None:
        args.output = Path(tempfile.mkdtemp(prefix="OpenKB-PROTOTYPE-"))
    if args.use_system_profile:
        os.environ.pop("OPENKB_PROBE_PROFILE", None)
    else:
        os.environ["OPENKB_PROBE_PROFILE"] = str(args.output.resolve() / "scratch-profile")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    for font in (assets() / "renderers" / "fonts").glob("*.otf"):
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
    app.setFont(QFont("Noto Sans CJK SC", 10))
    window = Window(args)
    window.show()
    app.exec()
