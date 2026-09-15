"""Quiet provenance and explicit reset actions for directly editable settings."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from openkb.desktop.form_controls import FocusComboBox

SOURCES = {
    "kb": "本库",
    "global": "全局",
    "default": "内置默认",
    "environment": "启动环境",
    "unset": "未设置",
}


class SettingActions(QWidget):
    """Keep merge-patch intent separate from the value and its source."""

    undoRequested = Signal()

    def __init__(self, label="此项设置", parent=None):
        super().__init__(parent)
        self._source = ""
        self.action = FocusComboBox(self)
        self.action.addItems(["不变", "设置", "清除覆盖"])
        self.action.hide()  # The patch state is driven by editing or the explicit menu.
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.source = QLabel()
        self.source.setObjectName("settingSource")
        row.addWidget(self.source, 1, Qt.AlignmentFlag.AlignVCenter)
        self.more = QToolButton()
        self.more.setObjectName("settingMenu")
        self.more.setText("⋯")
        self.more.setAccessibleName(label + "的更多操作")
        self.more.setToolTip("恢复默认或撤销本项修改")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more)
        self.reset = menu.addAction("恢复继承 / 默认值")
        self.reset.setToolTip("保存后移除此处的设置，使用上一层或内置默认值")
        self.reset.triggered.connect(lambda: self.action.setCurrentIndex(2))
        self.undo = menu.addAction("撤销本项修改")
        self.undo.triggered.connect(self.undoRequested.emit)
        self.more.setMenu(menu)
        row.addWidget(self.more)
        self.action.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def load(self, source):
        self._source = SOURCES.get(source, source)
        self.action.setCurrentIndex(0)
        self.refresh()

    def refresh(self, *_):
        state = self.action.currentIndex()
        self.source.setText("保存后恢复" if state == 2 else "已修改" if state else self._source)
        self.source.setToolTip("当前值来自：" + self._source)
        self.undo.setEnabled(state != 0)
