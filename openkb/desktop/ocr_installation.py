"""Simple optional-runtime installation panel with explicit acquisition and cancellation."""

from pathlib import Path
from threading import Event

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openkb.application.ocr_installation import (
    install_ocr,
    prepare_ocr_install,
    read_ocr_installations,
)
from openkb.desktop.flow_layout import FlowLayout
from openkb.desktop.form_controls import FocusComboBox
from openkb.ocr.installations import NAMES, default_root


class OcrInstallationPanel(QWidget):
    installed = Signal(str)
    progress = Signal(str)

    def __init__(self, io):
        super().__init__()
        self.io = io
        self.stop = Event()
        self.busy = False
        body = QVBoxLayout(self)
        self.profile = FocusComboBox()
        for name, label in NAMES.items():
            self.profile.addItem(label, name)
        self.directory = QLineEdit(str(default_root()))
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.install_button = QPushButton("安装所选模型")
        self.offline_button = QPushButton("导入离线包")
        self.cancel_button = QPushButton("停止安装")
        self.cancel_button.setEnabled(False)
        buttons = FlowLayout()
        for b in (self.install_button, self.offline_button, self.cancel_button):
            buttons.addWidget(b)
        for w in (self.profile, self.directory, self.description, self.state):
            body.addWidget(w)
        body.addLayout(buttons)
        self.profile.currentIndexChanged.connect(self.preview)
        self.directory.editingFinished.connect(self.preview)
        self.install_button.clicked.connect(lambda: self.start())
        self.offline_button.clicked.connect(self.choose_offline)
        self.cancel_button.clicked.connect(self.stop.set)
        self.progress.connect(self.state.setText)
        self.preview()

    def preview(self):
        try:
            plan = prepare_ocr_install(self.profile.currentData(), Path(self.directory.text()))
            self.description.setText(
                f"{plan['label']}\n下载 {plan['download_bytes']:,} 字节。"
                f"模型、运行环境与缓存分别存入：{plan['destination']}\n"
                "来源：固定版本的官方 Python/PyPI 运行包；原生模型来自 PaddlePaddle，"
                "Intel 1.5 来自 zhaohb 的 ModelScope 与固定源码。点击安装才下载。"
            )
        except (ValueError, OSError):
            self.description.setText("此平台的安装包未就绪。")

    def choose_offline(self):
        path = QFileDialog.getExistingDirectory(self, "选择完整离线 OCR 包")
        if path:
            self.start(Path(path))

    def start(self, offline=None):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.install_button.setEnabled(False)
        self.offline_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        profile = self.profile.currentData()
        directory = Path(self.directory.text())
        self.state.setText("准备安装…")
        self.io.submit(
            lambda: install_ocr(
                profile,
                destination=directory,
                offline=offline,
                cancelled=self.stop.is_set,
                progress=lambda n, d, t: self.progress.emit(f"{n}：{d:,} / {t:,} 字节"),
            ),
            self.finished,
        )

    def finished(self, value, error):
        self.busy = False
        self.install_button.setEnabled(True)
        self.offline_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.state.setText(
            "安装未完成，可再次尝试。"
            if error
            else "已安装。选择此运行环境并保存设置后使用；设备会在识别时完整检查。"
        )
        if not error:
            self.installed.emit(value["id"])

    def closeEvent(self, event):
        self.stop.set()
        super().closeEvent(event)


def fill_installations(combo, selected=None):
    combo.clear()
    combo.addItem("使用高级配置中的已有环境", None)
    for row in read_ocr_installations():
        if row["state"] == "ready":
            combo.addItem(NAMES[row["profile"]], row["id"])
    index = combo.findData(selected)
    if index >= 0:
        combo.setCurrentIndex(index)
