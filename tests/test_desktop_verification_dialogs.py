"""Exercise the actual acceptance dialog driver before expensive native builds."""

import os
import subprocess
import sys

import pytest


def test_acceptance_can_confirm_a_real_message_box():
    pytest.importorskip("PySide6")
    environment = dict(os.environ)
    if sys.platform == "linux":
        environment["QT_QPA_PLATFORM"] = "offscreen"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from openkb.desktop.verification import create_application

app = create_application()
box = QMessageBox()
box.setWindowTitle('确认删除对话')
box.setText('删除验收用对话？')
box.setInformativeText('已导出的副本会保留。')
box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
box.setDefaultButton(QMessageBox.StandardButton.No)

def confirm():
    modal = QApplication.activeModalWidget()
    if isinstance(modal, QMessageBox) and modal.informativeText().startswith(
        '已导出的副本会保留。'
    ):
        modal.done(QMessageBox.StandardButton.Yes)

timer = QTimer()
timer.timeout.connect(confirm)
timer.start(20)
assert box.exec() == QMessageBox.StandardButton.Yes
""",
        ],
        env=environment,
        check=True,
        timeout=10,
    )
