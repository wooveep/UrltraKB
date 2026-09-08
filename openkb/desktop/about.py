"""Native, selectable release provenance and original license texts."""

from __future__ import annotations

import json
import zipfile

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.distribution import load_distribution


def _materials():
    release = load_distribution()
    text = json.dumps(release.summary(), ensure_ascii=False, indent=2)
    entries = []
    for file in release.files:
        if file.kind == "notice":
            if file.size <= 2 * 1024 * 1024:
                with release.open_file(file.name) as stream:
                    text = stream.read().decode("utf-8") + "\n\n" + text
        if file.kind == "licenses":
            with release.open_file(file.name) as stream, zipfile.ZipFile(stream) as archive:
                entries.extend(
                    (file.name, info.filename) for info in archive.infolist() if not info.is_dir()
                )
    return release, text, sorted(entries)


class AboutDialog(QDialog):
    def __init__(self, io, parent=None):
        super().__init__(parent)
        self.io = io
        self._closed = False
        self._selection = 0
        self.release = None
        self.setWindowTitle("关于 UrltraKB · 源码与许可")
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取发行信息…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        tabs = QTabWidget()
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        tabs.addTab(self.info, "版本与源码")
        licenses = QWidget()
        license_layout = QVBoxLayout(licenses)
        self.licenses = QComboBox()
        self.licenses.activated.connect(self._license)
        license_layout.addWidget(self.licenses)
        self.license_text = QPlainTextEdit()
        self.license_text.setReadOnly(True)
        license_layout.addWidget(self.license_text)
        tabs.addTab(licenses, "完整许可与版权")
        layout.addWidget(tabs)
        actions = QHBoxLayout()
        copy = QPushButton("复制版本、源码与校验信息")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.info.toPlainText()))
        actions.addWidget(copy)
        self.folder = QPushButton("打开发行材料目录")
        self.folder.setEnabled(False)
        self.folder.clicked.connect(self._folder)
        actions.addWidget(self.folder)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._finished)
        io.submit(_materials, self._loaded, obsolete=lambda: self._closed)

    def _finished(self, result):
        self._closed = True

    def _loaded(self, value, error):
        if error:
            self.status.setText(f"发行材料读取失败：{error}")
            return
        self.release, text, entries = value
        self.info.setPlainText(text)
        self.folder.setEnabled(self.release.root is not None)
        if self.release.root:
            location = (
                "完整许可随程序提供。源码与构建资料单独提供：\n"
                f"{self.release.source_archive.name}（校验信息见下方）。"
                if self.release.source_archive
                else "SHA256 来自发行清单。源码、构建资料及原始许可保存在该目录。"
            )
            self.status.setText(f"发行材料：{self.release.root}\n{location}")
            self.info.appendPlainText(f"\n发行材料目录：{self.release.root}")
        else:
            self.status.setText("开发环境：尚未配置匹配的发行材料，不能据此声称发行验收通过。")
        for archive, member in entries:
            self.licenses.addItem(member, (archive, member))
        if entries:
            self._license()

    def _license(self):
        if self.release is None or not self.licenses.currentData():
            return
        release = self.release
        archive_name, member = self.licenses.currentData()
        self._selection += 1
        selection = self._selection
        self.license_text.setPlainText("正在读取…")

        def read():
            with release.open_file(archive_name) as stream, zipfile.ZipFile(stream) as archive:
                if archive.getinfo(member).file_size > 10 * 1024 * 1024:
                    raise ValueError("文件过大，请从发行材料目录打开")
                return archive.read(member).decode("utf-8", errors="replace")

        self.io.submit(
            read,
            lambda text, error: self.license_text.setPlainText(str(error) if error else text),
            obsolete=lambda: self._closed or selection != self._selection,
        )

    def _folder(self):
        if self.release is not None and self.release.root is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.release.root)))
