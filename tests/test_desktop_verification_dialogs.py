"""Exercise the actual acceptance dialog driver before expensive native builds."""

import os
import subprocess
import sys

import pytest


def test_acceptance_can_confirm_repeated_async_message_boxes():
    pytest.importorskip("PySide6")
    environment = dict(os.environ)
    environment["URLTRAKB_VERIFY_TIMEOUT_TRACE"] = "1"
    if sys.platform == "linux":
        environment["QT_QPA_PLATFORM"] = "offscreen"
    subprocess.run(
        [
            sys.executable,
            "-c",
            r"""
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QMessageBox
from openkb.desktop.verification import create_application
from openkb.desktop.verification_dialogs import message_box

app = create_application()
parent = QDialog()
parent.show()
confirmed = []

def show():
    dispatcher.stop()
    box = QMessageBox(parent)
    box.setWindowTitle('确认删除对话')
    box.setText('删除验收用对话？')
    box.setInformativeText('已导出的副本会保留。')
    box.setDetailedText('验收报告\n' * 5)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    assert box.exec() == QMessageBox.StandardButton.Yes
    confirmed.append(True)

def confirm():
    modal = message_box('已导出的副本会保留。')
    if modal is not None:
        modal.done(QMessageBox.StandardButton.Yes)

timer = QTimer()
timer.timeout.connect(confirm)
timer.start(20)
dispatcher = QTimer()
dispatcher.timeout.connect(show)
for index in range(20):
    dispatcher.start(1)
    while len(confirmed) <= index:
        app.processEvents()
        time.sleep(0.001)
assert len(confirmed) == 20
""",
        ],
        env=environment,
        check=True,
        timeout=10,
    )
