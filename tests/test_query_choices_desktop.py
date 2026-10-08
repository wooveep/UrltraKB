"""Ordinary import exposes generic file options without a product/version form."""

import os
import subprocess
import sys

import pytest


def test_import_form_has_no_product_or_version_assumptions():
    pytest.importorskip("PySide6")
    script = """
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget
from openkb.desktop.views import source_metadata_form
app = QApplication([])
root = QWidget()
layout = QVBoxLayout(root)
window = SimpleNamespace()
source_metadata_form(window, layout)
assert not root.findChildren(QLineEdit)
assert not hasattr(window, 'source_metadata_fields')
assert window.remote_assets.count() == 3
assert window.remote_assets.currentData() is None
assert all('产品' not in label.text() and '版本' not in label.text()
           for label in root.findChildren(QLabel))
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        timeout=15,
        capture_output=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
