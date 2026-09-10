"""Page composition around the existing native controls and application interfaces."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.desktop.drawer import Drawer
from openkb.desktop.flow_layout import FlowLayout
from openkb.desktop.fonts import text_font
from openkb.desktop.location import LocationLabel
from openkb.desktop.reader import MarkdownView
from openkb.desktop.shell import PAGES, action


def page():
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    return widget, layout


def path_label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setToolTip(text)
    return label


class Workspaces:
    def __init__(self, window):
        self.window = window
        self.hosts = {}
        self.panels = {}
        self._narrow = False
        self._directory_wide = True
        self._directory_narrow = False
        for name in PAGES:
            widget, layout = page()
            window.shell.stack.addWidget(widget)
            self.hosts[name] = layout
        self._overview()
        self._documents()
        self._knowledge()
        self._conversations()
        self._tasks()
        self._settings()
        self.empty = {}
        for name in ("资料", "产物", "知识", "对话"):
            hint = path_label("请先打开或新建知识库。")
            self.hosts[name].insertWidget(0, hint)
            self.empty[name] = hint
        window.workspaces = self
        self.reset()

    def _overview(self):
        outer = self.hosts["概览"]
        content, layout = page()
        content.setMaximumWidth(960)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(content, 1)
        row.addStretch()
        outer.addLayout(row, 1)
        layout.setContentsMargins(20, 32, 20, 20)
        title = QLabel("知识，从这里开始")
        title.setObjectName("welcomeTitle")
        layout.addWidget(title)
        layout.addWidget(path_label("将原始资料整理为相互关联的知识，继续阅读、提问与创作。"))
        row = FlowLayout()
        for label, callback in (
            ("新建知识库", self.window._create_kb),
            ("打开知识库", self.window._choose_kb),
        ):
            row.addWidget(action(label, callback))
        layout.addLayout(row)
        self.overview_location = LocationLabel("尚未打开知识库")
        layout.addWidget(self.overview_location)
        self.stats = QLabel("打开知识库后，在这里查看资料、知识与最近活动。")
        self.stats.setObjectName("overviewStats")
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        self.recent = path_label("")
        layout.addWidget(self.recent)
        self.shortcuts = QWidget()
        quick = FlowLayout(self.shortcuts)
        quick.setContentsMargins(0, 0, 0, 0)
        for label, target in (("导入资料", "资料"), ("阅读知识", "知识"), ("开始对话", "对话")):
            quick.addWidget(
                action(label, lambda checked=False, name=target: self.window.shell.navigate(name))
            )
        layout.addWidget(self.shortcuts)
        layout.addStretch()

    def _documents(self):
        row = FlowLayout()
        self.import_controls = QWidget()
        self.import_controls.setLayout(row)
        for label, callback in (
            ("导入文件", self.window._import_files),
            ("导入目录", self.window._import_directory),
            ("导入网址", self.window._import_urls),
        ):
            row.addWidget(action(label, callback))
        self.hosts["资料"].addWidget(self.import_controls)

    def _knowledge(self):
        w = self.window
        layout = self.hosts["知识"]
        row = FlowLayout()
        self.directory_toggle = action("知识目录", self.toggle_directory)
        self.context_toggle = action("来源与链接", self.toggle_context)
        for button in (self.directory_toggle, self.context_toggle):
            button.setCheckable(True)
            row.addWidget(button)
        row.addWidget(action("检查与修复…", w._maintenance))
        row.addWidget(action("刷新", w._refresh_current))
        w.zoom = QComboBox()
        w.zoom.setAccessibleName("内容缩放")
        for scale in (0.75, 1, 1.5, 2, 4):
            w.zoom.addItem(f"{scale:.0%}", scale)
        w.zoom.setCurrentIndex(1)
        w.zoom.activated.connect(w._presentation_changed)
        row.addWidget(w.zoom)
        layout.addLayout(row)
        w.location = LocationLabel("尚未选择知识页面")
        layout.addWidget(w.location)
        self.reading = QSplitter()
        w.pages = QTreeWidget()
        w.pages.setHeaderLabel("知识目录")
        w.pages.setMinimumWidth(160)
        w.pages.setMaximumWidth(320)
        w.pages.itemActivated.connect(w._activate_page)
        self.reading.addWidget(w.pages)
        w.tabs = QTabWidget()
        w.reader = MarkdownView()
        w.reader.anchorClicked.connect(w._follow_link)
        w.tabs.addTab(w.reader, "阅读")
        editor_panel, editor_layout = page()
        w.editor = QPlainTextEdit()
        w.editor.setFont(text_font(15, code=True))
        w.editor.setPlaceholderText("选择页面后编辑正文；原有元数据会保留。")
        w.editor.textChanged.connect(w._keep_draft)
        editor_layout.addWidget(w.editor, 1)
        buttons = FlowLayout()
        w.save_button = action("保存正文", w._save_page)
        for button in (
            w.save_button,
            action("查看最新版本 / 处理冲突", w._review_draft),
            action("导出草稿…", w._export_draft),
        ):
            buttons.addWidget(button)
        editor_layout.addLayout(buttons)
        w.tabs.addTab(editor_panel, "编辑")
        self.reading.addWidget(w.tabs)
        from openkb.desktop.page_context import PageContextView

        w.page_context = PageContextView(w)
        w.page_context.setMinimumWidth(180)
        self.reading.setStretchFactor(1, 1)
        self.reading.setSizes([210, 800])
        reading_host, reading_layout = page()
        reading_layout.addWidget(self.reading)
        layout.addWidget(reading_host, 1)
        self.context_drawer = Drawer(reading_host, "来源与链接", width=380)
        self.context_drawer.set_content(w.page_context)
        self.context_drawer.openedChanged.connect(self.context_toggle.setChecked)
        self.resize_reading(False)

    def toggle_directory(self):
        if self._narrow:
            self._directory_narrow = self.directory_toggle.isChecked()
        else:
            self._directory_wide = self.directory_toggle.isChecked()
        self.resize_reading(self._narrow)

    def toggle_context(self):
        # On narrow windows secondary panes share the reading space, never stack up.
        visible = self.context_toggle.isChecked()
        if visible and self._narrow:
            self._directory_narrow = False
            self.resize_reading(True)
        self.context_drawer.set_open(visible)

    def resize_reading(self, narrow):
        if narrow != self._narrow:
            self._directory_narrow = False
            self.context_drawer.set_open(False, immediate=True)
            self.context_toggle.setChecked(False)
        self._narrow = narrow
        visible = self._directory_narrow if narrow else self._directory_wide
        self.window.pages.setVisible(visible)
        self.directory_toggle.setChecked(visible)
        if visible and narrow:
            self.context_drawer.set_open(False, immediate=True)
            self.context_toggle.setChecked(False)

    def _conversations(self):
        from openkb.desktop.conversation_view import ConversationView, QuestionEdit

        w = self.window
        layout = self.hosts["对话"]
        row = QHBoxLayout()
        w.conversation_title = QLabel("新对话")
        w.conversation_title.setObjectName("conversationTitle")
        w.conversation_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(w.conversation_title, 1)
        self.new_chat = action("新对话", lambda: w.conversations.new())
        row.addWidget(self.new_chat)
        row.addStretch()
        self.history_toggle = action("对话历史", self.toggle_history)
        self.history_toggle.setCheckable(True)
        row.addWidget(self.history_toggle)
        layout.addLayout(row)
        w.conversation_notice = path_label("")
        w.conversation_notice.setObjectName("muted")
        w.conversation_notice.hide()
        layout.addWidget(w.conversation_notice)
        self.chat_stack = QTabWidget()
        self.chat_stack.tabBar().hide()
        chat_page, chat_layout = page()
        centered = QHBoxLayout()
        centered.addStretch()
        column, column_layout = page()
        column.setMaximumWidth(860)
        centered.addWidget(column, 1)
        centered.addStretch()
        chat_layout.addLayout(centered, 1)
        self.chat_welcome, welcome = page()
        welcome.addStretch()
        title = QLabel("有什么想了解的？")
        title.setObjectName("chatWelcome")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.addWidget(title)
        hint = QLabel("从你的知识库出发，连接资料与想法。")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.addWidget(hint)
        welcome.addStretch()
        column_layout.addWidget(self.chat_welcome, 1)
        w.chat = ConversationView()
        w.chat.anchorClicked.connect(w._follow_link)
        column_layout.addWidget(w.chat, 1)
        w.chat.textChanged.connect(self.conversation_content)
        self.chat_stack.addTab(chat_page, "当前对话")
        layout.addWidget(self.chat_stack, 1)
        self.history_drawer = Drawer(self.chat_stack, "对话历史", width=640)
        self.history_drawer.openedChanged.connect(self.history_toggle.setChecked)
        composer_row = QHBoxLayout()
        composer_row.addStretch()
        composer_column, composer_layout = page()
        composer_column.setMaximumWidth(860)
        composer = QFrame()
        composer.setObjectName("composer")
        composer_body = QVBoxLayout(composer)
        composer_body.setContentsMargins(16, 12, 14, 10)
        composer_body.setSpacing(4)
        w.question = QuestionEdit()
        composer_body.addWidget(w.question)
        controls = QHBoxLayout()
        self.autosave_hint = QLabel("对话自动保存")
        self.autosave_hint.setObjectName("muted")
        controls.addWidget(self.autosave_hint)
        controls.addStretch()
        w.ask_button = action("发送", w._ask)
        w.ask_button.setObjectName("sendButton")
        w.ask_button.setToolTip("发送 · Enter")
        controls.addWidget(w.ask_button)
        w.stop_answer = action("停止", lambda: w.conversations.stop())
        controls.addWidget(w.stop_answer)
        w.stop_answer.hide()
        composer_body.addLayout(controls)
        w.question.submitted.connect(w._ask)
        composer_layout.addWidget(composer)
        hint = QLabel("Enter 发送 · Shift + Enter 换行")
        hint.setObjectName("composerHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        composer_layout.addWidget(hint)
        composer_row.addWidget(composer_column, 1)
        composer_row.addStretch()
        layout.addLayout(composer_row)
        from openkb.desktop.conversations import Conversations

        w.conversations = Conversations(w)
        self.conversation_content()

    def conversation_content(self):
        empty = self.window.chat.toPlainText() in {
            "",
            "开始新对话。",
            "在当前知识库开始问答，或选择已有对话。",
        }
        self.chat_welcome.setVisible(empty)
        self.window.chat.setVisible(not empty)

    def toggle_history(self):
        self.activate("对话")
        if "对话" in self.panels and self.history_toggle.isChecked():
            self.panels["对话"][0].reload()
        self.history_drawer.set_open(self.history_toggle.isChecked())

    def show_answer(self):
        self.history_drawer.set_open(False)

    def _tasks(self):
        w = self.window
        layout = self.hosts["任务"]
        self.task_tabs = QTabWidget()
        task_page, tasks = page()
        w.task_table = QTableWidget(0, 6)
        w.task_table.setHorizontalHeaderLabels(
            ["知识库", "操作", "状态", "逐项结果", "阶段 / 错误", "阶段进度"]
        )
        w.task_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        w.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        w.task_table.horizontalHeader().setStretchLastSection(True)
        w.task_table.setColumnWidth(3, 230)
        w.task_table.setColumnWidth(5, 240)
        w.task_table.itemSelectionChanged.connect(w._select_task)
        tasks.addWidget(w.task_table, 2)
        row = FlowLayout()
        for label, callback in (
            ("安全停止所选任务", w._stop_selected),
            ("查看所选任务结果", w._task_details),
        ):
            row.addWidget(action(label, callback))
        tasks.addLayout(row)
        row = FlowLayout()
        self.retry = action("核对成果并重试所选任务…", w._retry_selected)
        self.clear = action("清理所选任务摘要…", w._clear_selected_history)
        row.addWidget(self.retry)
        row.addWidget(self.clear)
        tasks.addLayout(row)
        self.task_owner = path_label("选择任务以查看其所属知识库与保留产物。")
        tasks.addWidget(self.task_owner)
        w.artifacts = QListWidget()
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        w.artifacts.itemActivated.connect(
            lambda item: QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))
        )
        tasks.addWidget(w.artifacts, 1)
        self.task_tabs.addTab(task_page, "知识库任务 · 所有知识库")
        from openkb.desktop.watch import WatchDialog

        self.watches = WatchDialog(w.watch_registry, w.kb, w)
        self.watches.setWindowFlags(Qt.WindowType.Widget)
        self.task_tabs.addTab(self.watches, "目录监听 · 持续活动")
        layout.addWidget(self.task_tabs, 1)

    def _settings(self):
        w = self.window
        layout = self.hosts["设置"]
        row = QHBoxLayout()
        row.addWidget(QLabel("外观"))
        w.theme = QComboBox()
        w.theme.setAccessibleName("应用主题")
        for label, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            w.theme.addItem(label, value)
        row.addWidget(w.theme)
        row.addStretch()
        row.addWidget(action("关于 UrltraKB", w._about))
        layout.addLayout(row)
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setAccessibleName("设置范围")
        self.settings_tabs.currentChanged.connect(self.refresh_settings)
        layout.addWidget(self.settings_tabs, 1)

    def refresh_settings(self):
        selected = self.settings_tabs.currentWidget()
        for key in ("全局默认", "当前知识库"):
            if key in self.panels:
                panel, scroll = self.panels[key]
                if scroll is selected:
                    panel.reload()

    def embed(self, key, panel, layout=None):
        panel.setWindowFlags(Qt.WindowType.Widget)
        panel.setMinimumSize(0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(panel)
        if layout is not None:
            layout.addWidget(scroll, 1)
        self.panels[key] = (panel, scroll)
        return scroll

    def reset(self):
        # Retire callbacks before changing the visible owner. Running tasks are independent.
        for key, (panel, scroll) in list(self.panels.items()):
            if key == "全局默认":
                continue
            panel._closed = True
            panel.done(QDialog.DialogCode.Rejected)
            scroll.hide()
            scroll.setParent(self.window)
            del self.panels[key]
        while self.chat_stack.count() > 1:
            self.chat_stack.removeTab(1)
        while self.settings_tabs.count() > 1:
            self.settings_tabs.removeTab(1)
        self.history_drawer.set_open(False, immediate=True)
        self.history_drawer.set_content(None)
        self.context_drawer.set_open(False, immediate=True)
        enabled = self.window.kb is not None
        self.shortcuts.setEnabled(enabled)
        self.import_controls.setEnabled(enabled)
        self.watches.root = self.window.kb
        self.watches.start_button.setEnabled(enabled)
        self.new_chat.setEnabled(enabled)
        self.history_toggle.setEnabled(enabled)
        self.window.conversations.update_controls()
        self.window.save_button.setEnabled(False)
        for hint in self.empty.values():
            hint.setVisible(not enabled)
        self.overview_location.setText(str(self.window.kb) if enabled else "尚未打开知识库")
        self.stats.setText(
            "正在读取知识库统计…" if enabled else "打开知识库后，在这里查看资料、知识与最近活动。"
        )
        self.recent.clear()
        self.window.shell.navigate("概览")

    def activate(self, name):
        if name != "对话":
            self.history_drawer.set_open(False, immediate=True)
        if name != "知识":
            self.context_drawer.set_open(False, immediate=True)
        w = self.window
        if name == "设置":
            from openkb.desktop.settings import SettingsDialog

            for key, kb in (("全局默认", None), ("当前知识库", w.kb)):
                if key in self.panels or (key == "当前知识库" and kb is None):
                    continue
                panel = SettingsDialog(w.io, kb, w)
                panel.buttons.button(QDialogButtonBox.StandardButton.Close).hide()
                self.settings_tabs.addTab(self.embed(key, panel), key)
            self.refresh_settings()
        if w.kb is None:
            return
        if name == "资料" and name not in self.panels:
            from openkb.desktop.documents import DocumentsDialog

            self.embed(name, DocumentsDialog(w, w.kb), self.hosts[name])
        elif name == "产物" and name not in self.panels:
            from openkb.desktop.artifacts import ArtifactsDialog

            self.embed(name, ArtifactsDialog(w, w.kb), self.hosts[name])
            w._presentation_changed()
        elif name == "对话" and self.history_toggle.isChecked() and name not in self.panels:
            from openkb.desktop.sessions import SessionsDialog

            panel = SessionsDialog(w, w.kb)
            panel.embedded = True
            self.history_drawer.set_content(self.embed(name, panel))

    def overview_loaded(self, info, status):
        self.stats.setText(
            f"{info['document_count']} 份资料    ·    "
            f"{len(info['concepts'])} 个概念    ·    {len(info['entities'])} 个实体"
        )
        self.recent.setText(
            f"最近编译：{status['last_compile'] or '暂无记录'}\n"
            f"最近检查：{status['last_lint'] or '暂无记录'}"
        )

    def refresh_inventory(self, task):
        for key, (panel, _) in self.panels.items():
            if panel._closed:
                continue
            if (key == "资料" and task.operation in {"ImportFile", "ImportUrl"}) or (
                key == "对话" and task.operation == "ContinueConversation"
            ):
                panel.reload(preserve_result=bool(panel.details.toPlainText()))
            elif key == "产物" and task.operation in {
                "AskQuestion",
                "ContinueConversation",
                "CheckKnowledge",
            }:
                panel.refresh()
