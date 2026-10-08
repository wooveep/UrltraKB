"""Exercise the actual Qt scope selector without a running desktop window."""

import os
import subprocess
import sys

import pytest


def test_document_candidates_are_visible_and_only_change_scope_when_selected():
    pytest.importorskip("PySide6")
    script = """
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from openkb.desktop.views import ViewPicker
app = QApplication([])
events = []
empty = SimpleNamespace(clear=lambda: None, reset=lambda: None)
window = SimpleNamespace(
    kb=object(), view_id=None, page=None, _page_request_id=0,
    _keep_draft=lambda: events.append('draft-kept'),
    page_context=empty, editor=empty, conversations=empty, workspaces=empty,
    reader=SimpleNamespace(show_temporary=lambda text: None),
    _refresh=lambda: events.append('refresh'),
)
picker = ViewPicker(window)
picker.set_candidates([{
    'view_id': 'confirmed-view', 'product': 'CNware WinStack',
    'versions': ['9.4.0'], 'sources': ['安装手册.pdf', '运维手册.pdf'],
    'reason': '产品简称尚未确认',
}])
assert window.view_id is None and picker.currentData() is None and not events
index = picker.findData('confirmed-view')
assert '安装手册.pdf' in picker.itemText(index) and '运维手册.pdf' in picker.itemText(index)
picker.setCurrentIndex(index)
picker.activated.emit(index)
assert window.view_id == 'confirmed-view' and events == ['draft-kept', 'refresh']
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        timeout=15,
        capture_output=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
