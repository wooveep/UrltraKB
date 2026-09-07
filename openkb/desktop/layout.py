"""Qt widget layout and signal wiring for the native workbench."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.desktop.reader import MarkdownView


def _action(window, toolbar, label, callback):
    action = QAction(label, window)
    action.triggered.connect(callback)
    toolbar.addAction(action)
    return action


def build_workbench(window):
    toolbar = QToolBar("知识库与资料", window)
    toolbar.setMovable(False)
    window.addToolBar(toolbar)
    _action(window, toolbar, "新建知识库", window._create_kb)
    _action(window, toolbar, "打开知识库", window._choose_kb)
    toolbar.addSeparator()
    window.kbs = QComboBox()
    window.kbs.setMinimumWidth(270)
    window.kbs.setPlaceholderText("选择知识库")
    window.kbs.activated.connect(lambda: window.open_knowledge_base(Path(window.kbs.currentData())))
    toolbar.addWidget(window.kbs)
    _action(window, toolbar, "导入文件", window._import_files)
    _action(window, toolbar, "导入目录", window._import_directory)
    _action(window, toolbar, "导入网址", window._import_urls)
    _action(window, toolbar, "资料管理", window._documents)
    _action(window, toolbar, "刷新", window._refresh_current)
    settings = window.menuBar().addMenu("设置")
    settings.addAction("当前知识库…", lambda: window._settings(global_defaults=False))
    settings.addAction("全局默认…", lambda: window._settings(global_defaults=True))
    maintenance = window.menuBar().addMenu("维护")
    maintenance.addAction("当前知识库检查与修复…", window._maintenance)
    maintenance.addAction("诊断指定知识库…", lambda: window._diagnose())
    maintenance.addAction("目录监听…", window._watch)
    artifacts = window.menuBar().addMenu("生成与产物")
    artifacts.addAction("Skill、幻灯片、图谱与产物管理…", window._manage_artifacts)
    _action(window, toolbar, "退出", window.request_quit)
    window.addToolBarBreak()
    reading_toolbar = QToolBar("阅读显示", window)
    window.addToolBar(reading_toolbar)
    window.theme = QComboBox()
    window.theme.addItems(["浅色阅读", "深色阅读"])
    window.zoom = QComboBox()
    for scale in (0.75, 1, 1.5, 2, 4):
        window.zoom.addItem(f"{scale:.0%}", scale)
    window.zoom.setCurrentIndex(1)
    window.theme.activated.connect(window._presentation_changed)
    window.zoom.activated.connect(window._presentation_changed)
    reading_toolbar.addWidget(window.theme)
    reading_toolbar.addWidget(window.zoom)

    window.pages = QTreeWidget()
    window.pages.setHeaderLabel("知识页面")
    window.pages.itemActivated.connect(window._activate_page)
    window.pages.setMinimumWidth(210)
    window.tabs = QTabWidget()
    window.reader = MarkdownView()
    window.reader.anchorClicked.connect(window._follow_link)
    window.tabs.addTab(window.reader, "阅读")
    editor_panel = QWidget()
    editor_layout = QVBoxLayout(editor_panel)
    window.editor = QPlainTextEdit()
    window.editor.setPlaceholderText("选择页面后编辑正文；原有元数据会保留。")
    window.editor.textChanged.connect(window._keep_draft)
    editor_layout.addWidget(window.editor)
    window.save_button = QPushButton("保存正文")
    window.save_button.clicked.connect(window._save_page)
    editor_layout.addWidget(window.save_button)
    review = QPushButton("查看最新版本 / 处理冲突")
    review.clicked.connect(window._review_draft)
    editor_layout.addWidget(review)
    export = QPushButton("导出草稿…")
    export.clicked.connect(window._export_draft)
    editor_layout.addWidget(export)
    window.tabs.addTab(editor_panel, "编辑")
    window.chat = MarkdownView()
    window.chat.anchorClicked.connect(window._follow_link)
    window.tabs.addTab(window.chat, "问答与对话")
    center = QWidget()
    center_layout = QVBoxLayout(center)
    window.location = QLabel("尚未打开知识库")
    window.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    center_layout.addWidget(window.location)
    center_layout.addWidget(window.tabs, 1)
    ask_options = QHBoxLayout()
    window.mode = QComboBox()
    window.mode.addItems(["一次问答", "对话"])
    window.sessions = QComboBox()
    window.sessions.addItem("新对话", None)
    window.sessions.activated.connect(window._load_conversation)
    window.save_answer = QCheckBox("保存回答")
    ask_options.addWidget(window.mode)
    ask_options.addWidget(window.sessions, 1)
    ask_options.addWidget(window.save_answer)
    manage_sessions = QPushButton("管理对话…")
    manage_sessions.clicked.connect(window._manage_sessions)
    ask_options.addWidget(manage_sessions)
    center_layout.addLayout(ask_options)
    window.question = QPlainTextEdit()
    window.question.setPlaceholderText("向当前知识库提问…")
    window.question.setMaximumHeight(85)
    center_layout.addWidget(window.question)
    window.ask_button = QPushButton("发送")
    window.ask_button.clicked.connect(window._ask)
    center_layout.addWidget(window.ask_button)
    splitter = QSplitter()
    splitter.addWidget(window.pages)
    splitter.addWidget(center)
    splitter.setStretchFactor(1, 1)
    window.setCentralWidget(splitter)

    tasks_panel = QWidget()
    tasks_layout = QVBoxLayout(tasks_panel)
    window.task_table = QTableWidget(0, 5)
    window.task_table.setHorizontalHeaderLabels(
        ["知识库", "操作", "状态", "逐项结果", "阶段 / 错误"]
    )
    window.task_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    window.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    window.task_table.horizontalHeader().setStretchLastSection(True)
    window.task_table.itemSelectionChanged.connect(window._select_task)
    tasks_layout.addWidget(window.task_table)
    stop = QPushButton("安全停止所选任务")
    stop.clicked.connect(window._stop_selected)
    tasks_layout.addWidget(stop)
    details = QPushButton("查看所选任务结果")
    details.clicked.connect(window._task_details)
    tasks_layout.addWidget(details)
    dock = QDockWidget("任务 · 所有知识库", window)
    dock.setWidget(tasks_panel)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    window.resizeDocks([dock], [170], Qt.Orientation.Vertical)
    window.artifacts = QListWidget()
    window.artifacts.itemDoubleClicked.connect(
        lambda item: QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))
    )
    artifacts_dock = QDockWidget("任务产物 · 双击打开", window)
    artifacts_dock.setWidget(window.artifacts)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, artifacts_dock)
