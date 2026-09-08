"""Responsive workbench navigation; page contents retain their own state."""

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openkb.desktop.brand import NAME, mark_icon
from openkb.desktop.navigation_icons import navigation_icon

PAGES = ("概览", "资料", "知识", "对话", "产物", "任务", "设置")


def action(label, callback, parent=None):
    button = QPushButton(label, parent)
    button.setAccessibleName(label)
    button.setToolTip(label)
    button.clicked.connect(callback)
    return button


class WorkbenchShell(QWidget):
    def __init__(self, window, preferences):
        super().__init__(window)
        self.window, self.preferences = window, preferences
        self.expanded = preferences.value("navigation/expanded", True, type=bool)
        self._compact = False
        self._task_counts = (0, 0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.navigation = QFrame()
        self.navigation.setObjectName("navigation")
        nav = QVBoxLayout(self.navigation)
        nav.setContentsMargins(10, 14, 10, 12)
        nav.setSpacing(4)
        self.brand_row = QWidget()
        brand = QHBoxLayout(self.brand_row)
        brand.setContentsMargins(6, 0, 0, 12)
        self.mark = QLabel()
        self.mark.setPixmap(mark_icon().pixmap(24, 24))
        self.mark.setAccessibleName("UrltraKB 标志")
        brand.addWidget(self.mark)
        self.brand_name = QLabel(NAME)
        self.brand_name.setObjectName("brand")
        brand.addWidget(self.brand_name, 1)
        self.toggle = QToolButton()
        self.toggle.setText("☰")
        self.toggle.setFixedSize(32, 34)
        self.toggle.clicked.connect(self.toggle_navigation)
        brand.addWidget(self.toggle)
        nav.addWidget(self.brand_row)
        self.buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for name in PAGES:
            if name == "设置":
                nav.addStretch()
            button = QPushButton()
            button.setText(name)
            button.setIcon(navigation_icon(name))
            button.setIconSize(QSize(19, 19))
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setCheckable(True)
            button.setMinimumHeight(42)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda checked=False, page=name: self.navigate(page))
            group.addButton(button)
            nav.addWidget(button)
            self.buttons[name] = button
        layout.addWidget(self.navigation)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        main = QVBoxLayout(workspace)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self.top = QFrame()
        self.top.setObjectName("topbar")
        top = self.top_layout = QHBoxLayout(self.top)
        top.setContentsMargins(26, 14, 22, 14)
        self.title = QLabel("概览")
        self.title.setObjectName("pageTitle")
        self.title.setAccessibleName("当前工作区")
        top.addWidget(self.title)
        top.addSpacing(12)
        window.kbs = QComboBox()
        window.kbs.setAccessibleName("当前知识库")
        window.kbs.setPlaceholderText("选择知识库")
        window.kbs.setMinimumWidth(150)
        window.kbs.setMaximumWidth(320)
        window.kbs.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        window.kbs.activated.connect(self._switch_kb)
        top.addWidget(window.kbs)
        top.addStretch()
        self.task_status = action("任务 · 0 运行", lambda: self.navigate("任务"))
        self.task_status.setAccessibleName("查看任务")
        top.addWidget(self.task_status)
        more = QToolButton()
        more.setText("⋯")
        more.setAccessibleName("应用菜单")
        more.setToolTip("应用菜单")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("知识库管理", window._knowledge_bases)
        menu.addAction("新建知识库", window._create_kb)
        menu.addAction("打开知识库", window._choose_kb)
        menu.addAction("诊断指定知识库…", lambda: window._diagnose())
        menu.addAction("关于 UrltraKB · 源码与许可…", window._about)
        menu.addSeparator()
        menu.addAction("退出", window.request_quit)
        more.setMenu(menu)
        top.addWidget(more)
        main.addWidget(self.top)
        content = self.content_layout = QVBoxLayout()
        content.setContentsMargins(26, 12, 26, 18)
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        main.addLayout(content, 1)
        layout.addWidget(workspace, 1)
        self.update_navigation()

    def _switch_kb(self):
        from pathlib import Path

        if value := self.window.kbs.currentData():
            self.window.open_knowledge_base(Path(value))

    def navigate(self, name):
        if self.window._quitting and name != "任务":
            return
        self.title.setText(name)
        self.buttons[name].setChecked(True)
        self.stack.setCurrentIndex(PAGES.index(name))
        self.window.workspaces.activate(name)

    def toggle_navigation(self):
        # A narrow-window override is temporary; it never writes the wide preference.
        if self.width() < 1080:
            self._compact = not self._compact
        else:
            self.expanded = not self.expanded
            self.preferences.setValue("navigation/expanded", self.expanded)
            self.preferences.sync()
        self.update_navigation()

    def update_navigation(self):
        compact = self._compact if self.width() < 1080 else not self.expanded
        self.navigation.setFixedWidth(64 if compact else 232)
        self.brand_name.setVisible(not compact)
        self.mark.setVisible(not compact)
        self.brand_row.layout().setContentsMargins(0 if compact else 6, 0, 0, 12)
        label = "展开导航" if compact else "收起导航"
        self.toggle.setAccessibleName(label)
        self.toggle.setToolTip(label)
        for name, button in self.buttons.items():
            button.setText("" if compact else name)

    def set_task_status(self, running, attention):
        self._task_counts = running, attention
        self.task_status.setText(
            f"任务 · {running + attention}"
            if self.width() < 1000
            else f"任务 · {running} 运行 · {attention} 需关注"
        )

    def resizeEvent(self, event):
        if (event.oldSize().width() < 1080) != (event.size().width() < 1080):
            self._compact = event.size().width() < 1080
        self.update_navigation()
        narrow = event.size().width() < 1080
        margin = 12 if narrow else 26
        self.top_layout.setContentsMargins(margin, 10, margin, 10)
        self.content_layout.setContentsMargins(margin, 8 if narrow else 12, margin, 12)
        self.set_task_status(*self._task_counts)
        self.window.kbs.setMaximumWidth(220 if narrow else 320)
        if hasattr(self.window, "workspaces"):
            self.window.workspaces.resize_reading(event.size().width() < 1080)
        super().resizeEvent(event)
