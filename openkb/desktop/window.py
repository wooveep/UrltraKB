"""Native workbench: KB navigation, reading/editing, questions, and task observation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openkb.agent.chat_session import list_sessions
from openkb.application.conversations import read_conversation
from openkb.application.knowledge_bases import get_kb_list, initialize_kb, open_kb
from openkb.application.pages import Page, read_page
from openkb.config import GLOBAL_CONFIG_DIR, registered_kbs
from openkb.desktop.editor import DraftDialog, PageDraft
from openkb.desktop.io import LocalIO
from openkb.desktop.reader import MarkdownView
from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.page_ops import EDITABLE_SECTIONS
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import AskQuestion, ContinueConversation, ImportFile, SavePage
from openkb.runtime.tasks import TaskManager

_STATES = {
    "queued": "排队中",
    "waiting": "等待知识库",
    "running": "执行中",
    "stopping": "正在安全停止",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "意外中断",
    "blocked": "需要修复",
}
_OPERATIONS = {
    "SavePage": "保存页面",
    "AskQuestion": "问答",
    "ContinueConversation": "对话",
    "ImportFile": "导入资料",
}


class Workbench(QMainWindow):
    def __init__(self, *, history_dir: Path | None = None):
        super().__init__()
        self.setWindowTitle("OpenKB")
        self.resize(1320, 900)
        self.kb: Path | None = None
        self.page: Page | None = None
        self._drafts: dict[tuple[str, str], PageDraft] = {}
        self._task_questions: dict[str, str] = {}
        self._save_tasks: dict[str, tuple[tuple[str, str], str]] = {}
        self._seen_terminal: set[str] = set()
        self._page_request_id = 0
        self._open_request_id = 0
        self._conversation_request_id = 0
        self._chat_task: str | None = None
        self._last_chat_text = None
        self._quitting = False
        self.io = LocalIO(self)
        self.manager = TaskManager(history_dir=history_dir or GLOBAL_CONFIG_DIR / "desktop/tasks")
        self._build_window()
        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_tasks)
        self.timer.start(150)
        self.io.submit(registered_kbs, self._recent_loaded)
        self.reader.show_markdown(
            "# OpenKB\n\n打开已有知识库，或在您选择的位置创建一个。\n\n"
            "资料、页面、对话和任务始终归属于各自的知识库。",
            Path.cwd(),
        )

    def _action(self, toolbar, label, callback):
        action = QAction(label, self)
        action.triggered.connect(callback)
        toolbar.addAction(action)
        return action

    def _build_window(self):
        toolbar = QToolBar("知识库与资料", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self._action(toolbar, "新建知识库", self._create_kb)
        self._action(toolbar, "打开知识库", self._choose_kb)
        toolbar.addSeparator()
        self.kbs = QComboBox()
        self.kbs.setMinimumWidth(270)
        self.kbs.setPlaceholderText("选择知识库")
        self.kbs.activated.connect(lambda: self.open_knowledge_base(Path(self.kbs.currentData())))
        toolbar.addWidget(self.kbs)
        self._action(toolbar, "导入文件", self._import_files)
        self._action(toolbar, "导入目录", self._import_directory)
        self._action(toolbar, "刷新", self._refresh_current)
        self._action(toolbar, "退出", self.request_quit)
        self.addToolBarBreak()
        reading_toolbar = QToolBar("阅读显示", self)
        self.addToolBar(reading_toolbar)
        self.theme = QComboBox()
        self.theme.addItems(["浅色阅读", "深色阅读"])
        self.zoom = QComboBox()
        for scale in (0.75, 1, 1.5, 2, 4):
            self.zoom.addItem(f"{scale:.0%}", scale)
        self.zoom.setCurrentIndex(1)
        self.theme.activated.connect(self._presentation_changed)
        self.zoom.activated.connect(self._presentation_changed)
        reading_toolbar.addWidget(self.theme)
        reading_toolbar.addWidget(self.zoom)

        self.pages = QTreeWidget()
        self.pages.setHeaderLabel("知识页面")
        self.pages.itemActivated.connect(self._activate_page)
        self.pages.setMinimumWidth(210)
        self.tabs = QTabWidget()
        self.reader = MarkdownView()
        self.reader.anchorClicked.connect(self._follow_link)
        self.tabs.addTab(self.reader, "阅读")
        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("选择页面后编辑正文；原有元数据会保留。")
        self.editor.textChanged.connect(self._keep_draft)
        editor_layout.addWidget(self.editor)
        self.save_button = QPushButton("保存正文")
        self.save_button.clicked.connect(self._save_page)
        editor_layout.addWidget(self.save_button)
        review = QPushButton("查看最新版本 / 处理冲突")
        review.clicked.connect(self._review_draft)
        editor_layout.addWidget(review)
        export = QPushButton("导出草稿…")
        export.clicked.connect(self._export_draft)
        editor_layout.addWidget(export)
        self.tabs.addTab(editor_panel, "编辑")
        self.chat = MarkdownView()
        self.chat.anchorClicked.connect(self._follow_link)
        self.tabs.addTab(self.chat, "问答与对话")
        center = QWidget()
        center_layout = QVBoxLayout(center)
        self.location = QLabel("尚未打开知识库")
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        center_layout.addWidget(self.location)
        center_layout.addWidget(self.tabs, 1)
        ask_options = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["一次问答", "对话"])
        self.sessions = QComboBox()
        self.sessions.addItem("新对话", None)
        self.sessions.activated.connect(self._load_conversation)
        self.save_answer = QCheckBox("保存回答")
        ask_options.addWidget(self.mode)
        ask_options.addWidget(self.sessions, 1)
        ask_options.addWidget(self.save_answer)
        center_layout.addLayout(ask_options)
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText("向当前知识库提问…")
        self.question.setMaximumHeight(85)
        center_layout.addWidget(self.question)
        self.ask_button = QPushButton("发送")
        self.ask_button.clicked.connect(self._ask)
        center_layout.addWidget(self.ask_button)
        splitter = QSplitter()
        splitter.addWidget(self.pages)
        splitter.addWidget(center)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        tasks_panel = QWidget()
        tasks_layout = QVBoxLayout(tasks_panel)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["知识库", "操作", "状态", "逐项结果", "阶段 / 错误"]
        )
        self.task_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.task_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.itemSelectionChanged.connect(self._select_task)
        tasks_layout.addWidget(self.task_table)
        stop = QPushButton("安全停止所选任务")
        stop.clicked.connect(self._stop_selected)
        tasks_layout.addWidget(stop)
        dock = QDockWidget("任务 · 所有知识库", self)
        dock.setWidget(tasks_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [170], Qt.Orientation.Vertical)
        self.artifacts = QListWidget()
        self.artifacts.itemDoubleClicked.connect(
            lambda item: QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))
        )
        artifacts_dock = QDockWidget("任务产物 · 双击打开", self)
        artifacts_dock.setWidget(self.artifacts)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, artifacts_dock)

    def _build_tray(self):
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("OpenKB")
        menu = QMenu(self)
        menu.addAction("显示 OpenKB", self._show_window)
        menu.addAction("退出", self.request_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_window())
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _presentation_changed(self):
        for view in (self.reader, self.chat):
            view.set_presentation(
                dark=bool(self.theme.currentIndex()), scale=self.zoom.currentData()
            )

    def _error(self, error):
        if error:
            QMessageBox.warning(self, "操作未完成", str(error))
            return True
        return False

    def _recent_loaded(self, values, error):
        if not error:
            for label, path in values:
                self.kbs.addItem(f"{label} · {path}", str(path))
            self.kbs.setCurrentIndex(-1)

    def _choose_kb(self):
        path = QFileDialog.getExistingDirectory(self, "打开知识库")
        if path:
            self.open_knowledge_base(Path(path))

    def _create_kb(self):
        path = QFileDialog.getExistingDirectory(self, "选择新知识库的空目录")
        if path:
            if any(Path(path).iterdir()):
                self._error(ValueError("请选择空目录创建知识库；已有知识库请使用“打开”。"))
                return
            self.io.submit(
                lambda: initialize_kb(Path(path), seed_environment=False),
                lambda result, error: None
                if self._error(error)
                else self.open_knowledge_base(Path(path)),
            )

    def open_knowledge_base(self, path: Path):
        self._open_request_id += 1
        request_id = self._open_request_id
        if self.kb:
            self.kbs.setCurrentIndex(self.kbs.findData(str(self.kb)))
        self.statusBar().showMessage(f"正在打开 {path}；若有写入任务，将等待其安全完成。")
        self.io.submit(
            lambda: open_kb(path),
            lambda value, error: self._opened(value, error)
            if request_id == self._open_request_id
            else None,
            kb=path,
            exclusive=True,
            obsolete=lambda: request_id != self._open_request_id,
        )

    def _opened(self, root, error):
        if self._error(error):
            return
        self._keep_draft()
        self.kb, self.page = root, None
        self._page_request_id += 1
        self._chat_task = None
        self._conversation_request_id += 1
        self._last_chat_text = None
        self.reader.show_temporary("正在读取当前知识库…")
        self.chat.show_temporary("在当前知识库开始问答，或选择已有对话。")
        self.sessions.clear()
        self.sessions.addItem("新对话", None)
        self.question.clear()
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self.location.setText(str(root))
        self.setWindowTitle(f"{root.name} — OpenKB")
        index = self.kbs.findData(str(root))
        if index < 0:
            self.kbs.addItem(f"{root.name} · {root}", str(root))
            index = self.kbs.count() - 1
        self.kbs.setCurrentIndex(index)
        self._refresh()

    def _refresh(self):
        if self.kb is None:
            return
        root = self.kb

        def read():
            return (
                sorted(
                    p.relative_to(root / "wiki").as_posix() for p in (root / "wiki").rglob("*.md")
                ),
                list_sessions(root),
                get_kb_list(root),
            )

        def loaded(value, error):
            if self.kb != root or self._error(error):
                return
            pages, sessions, info = value
            self.pages.clear()
            groups = {}
            for path in pages:
                group = path.split("/")[0] if "/" in path else "知识库"
                if group not in groups:
                    groups[group] = QTreeWidgetItem(self.pages, [group])
                item = QTreeWidgetItem(groups[group], [Path(path).stem])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
            self.pages.expandToDepth(0)
            selected = self.sessions.currentData()
            self.sessions.clear()
            self.sessions.addItem("新对话", None)
            for session in sessions:
                self.sessions.addItem(session["title"] or session["id"], session["id"])
            found = self.sessions.findData(selected)
            self.sessions.setCurrentIndex(max(0, found))
            self.statusBar().showMessage(f"{root.name} · {info.get('document_count', 0)} 份资料")
            if self.page is None and (root / "wiki/index.md").exists():
                self.open_page("index.md")

        self.io.submit(read, loaded, kb=root)

    def _refresh_current(self):
        self._refresh()
        if self.page:
            self.open_page(self.page.path)

    def _activate_page(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.open_page(path)

    def open_page(self, path: str, anchor: str = ""):
        if self.kb is None:
            return
        root = self.kb
        self._page_request_id += 1
        request_id = self._page_request_id
        self._keep_draft()

        def loaded(page, error):
            if self.kb != root or request_id != self._page_request_id or self._error(error):
                return
            self.page = page
            editable = page.path.split("/")[0] in EDITABLE_SECTIONS
            self.save_button.setEnabled(editable)
            self.editor.setReadOnly(not editable)
            draft = self._drafts.get((str(root), page.path))
            self.editor.blockSignals(True)
            self.editor.setPlainText(draft.body if draft else page.body)
            self.editor.blockSignals(False)
            self.location.setText(f"{root}  /  {page.path}.md")
            self.reader.show_markdown(page.body, (root / "wiki" / page.path).parent, anchor=anchor)
            self.tabs.setCurrentIndex(0)

        self.io.submit(
            lambda: read_page(root, path),
            loaded,
            kb=root,
            obsolete=lambda: root != self.kb or request_id != self._page_request_id,
        )

    def _keep_draft(self):
        if self.kb and self.page:
            key = (str(self.kb), self.page.path)
            previous = self._drafts.get(key)
            if self.editor.toPlainText() == self.page.body:
                self._drafts.pop(key, None)
                return
            self._drafts[key] = PageDraft(
                self.editor.toPlainText(),
                previous.version if previous else self.page.version,
            )

    def _save_page(self):
        if not self.kb or not self.page:
            return
        self._keep_draft()
        key = (str(self.kb), self.page.path)
        draft = self._drafts.get(key, PageDraft(self.page.body, self.page.version))
        body, version = draft.body, draft.version
        task_id = self.manager.submit(self.kb, [SavePage(self.page.path, body, version)])
        self._save_tasks[task_id] = (key, body)
        self.statusBar().showMessage("保存任务已提交；出现版本冲突时草稿会保留。")

    def _review_draft(self):
        if not self.kb or not self.page:
            return
        root, path = self.kb, self.page.path
        self._keep_draft()

        def loaded(latest, error):
            if self.kb != root or not self.page or self.page.path != path or self._error(error):
                return
            self._keep_draft()
            draft = self._drafts.get(
                (str(root), path), PageDraft(self.page.body, self.page.version)
            )
            dialog = DraftDialog(latest, draft, self)
            if dialog.exec():
                self.page = latest
                self._drafts[(str(root), path)] = PageDraft(
                    dialog.draft.toPlainText(), latest.version
                )
                self.editor.setPlainText(dialog.draft.toPlainText())
                self._save_page()

        self.io.submit(lambda: read_page(root, path), loaded, kb=root)

    def _export_draft(self):
        if not self.page:
            return
        from openkb.locks import atomic_write_text

        path, _ = QFileDialog.getSaveFileName(self, "导出草稿", "draft.md", "Markdown (*.md)")
        if path:
            destination = Path(path).expanduser().resolve()
            if any((parent / ".openkb/config.yaml").is_file() for parent in destination.parents):
                self._error(ValueError("请导出到知识库之外；知识库页面请通过保存操作更新。"))
                return
            body = self.editor.toPlainText()
            self.io.submit(
                lambda: atomic_write_text(destination, body),
                lambda result, error: self._error(error),
            )

    def _import_files(self):
        if self.kb:
            files, _ = QFileDialog.getOpenFileNames(self, "导入资料")
            if files:
                self.manager.submit(
                    self.kb, [ImportFile(str(Path(path).resolve())) for path in files]
                )

    def _import_directory(self):
        if not self.kb:
            return
        path = QFileDialog.getExistingDirectory(self, "递归导入目录")
        if path:
            root = self.kb

            def loaded(files, error):
                if not self._error(error) and files:
                    self.manager.submit(root, [ImportFile(str(path)) for path in files])

            self.io.submit(
                lambda: sorted(
                    p.resolve()
                    for p in Path(path).rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
                ),
                loaded,
            )

    def _ask(self):
        if not self.kb or not self.question.toPlainText().strip():
            return
        question = self.question.toPlainText()
        request = (
            AskQuestion(question, self.save_answer.isChecked())
            if self.mode.currentIndex() == 0
            else ContinueConversation(question, self.sessions.currentData())
        )
        task_id = self.manager.submit(self.kb, [request])
        self._conversation_request_id += 1
        self._task_questions[task_id] = question
        self._chat_task = task_id
        self._last_chat_text = None
        self.chat.show_temporary("已排队，等待执行…")
        self.tabs.setCurrentIndex(2)
        self.question.clear()

    def _load_conversation(self):
        self._conversation_request_id += 1
        request_id = self._conversation_request_id
        if not self.kb or not self.sessions.currentData():
            self._chat_task = None
            self.chat.show_temporary("开始新对话。")
            return
        root, session_id = self.kb, self.sessions.currentData()
        self._chat_task = None
        self.chat.show_temporary("正在读取对话…")

        def loaded(session, error):
            if (
                self.kb != root
                or request_id != self._conversation_request_id
                or self.sessions.currentData() != session_id
                or self._error(error)
            ):
                return
            self._chat_task = None
            text = "\n\n".join(
                f"**你**\n\n{user}\n\n**OpenKB**\n\n{answer}" for user, answer in session.turns
            )
            self.chat.show_markdown(text, root / "wiki")
            self.tabs.setCurrentIndex(2)
            self.mode.setCurrentIndex(1)

        self.io.submit(
            lambda: read_conversation(root, session_id),
            loaded,
            kb=root,
            obsolete=lambda: root != self.kb or request_id != self._conversation_request_id,
        )

    def _follow_link(self, url: QUrl):
        if not self.kb:
            return
        if url.scheme() == "openkb":
            self.open_page(url.path(QUrl.ComponentFormattingOption.FullyDecoded), url.fragment())
        elif url.isLocalFile():
            path = Path(url.toLocalFile()).resolve()
            if path.is_relative_to(self.kb / "wiki") and path.suffix == ".md":
                self.open_page(str(path.relative_to(self.kb / "wiki")), url.fragment())
        elif not url.scheme() and url.fragment():
            from openkb.rendering.markdown import heading_anchor

            current = self.tabs.currentWidget()
            if isinstance(current, MarkdownView):
                current.scrollToAnchor(heading_anchor(url.fragment()))
        elif url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)

    def _selected_task(self):
        row = self.task_table.currentRow()
        return self.task_table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None

    def _select_task(self):
        task_id = self._selected_task()
        self.artifacts.clear()
        if task_id:
            for result in self.manager.get(task_id).results:
                self.artifacts.addItems(list(result.resources))

    def _stop_selected(self):
        task_id = self._selected_task()
        if task_id:
            self.manager.stop(task_id)

    def _poll_tasks(self):
        tasks = self.manager.tasks()
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [
                Path(task.kb_dir).name,
                _OPERATIONS.get(task.operation, task.operation),
                _STATES.get(task.state, task.state),
                f"成功 {task.succeeded} · 跳过 {task.skipped} · "
                f"失败 {task.failed} · 未处理 {task.unfinished}",
                task.error or task.stage,
            ]
            for column, value in enumerate(values):
                item = self.task_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.task_table.setItem(row, column, item)
                item.setText(value)
                item.setData(Qt.ItemDataRole.UserRole, task.id)
                item.setToolTip(task.kb_dir)
            if task.id == self._chat_task and self.kb and str(self.kb) == task.kb_dir:
                if task.text != self._last_chat_text:
                    self._last_chat_text = task.text
                    self.chat.show_temporary(
                        task.text or task.error or _STATES.get(task.state, task.state)
                    )
            if task.state in TERMINAL and task.id not in self._seen_terminal:
                self._seen_terminal.add(task.id)
                self._task_finished(task)
        self.task_table.blockSignals(False)
        if self._quitting:
            if (
                self.manager.join(0)
                and self.io.stopped()
                and all(view.rendering_stopped() for view in (self.reader, self.chat))
            ):
                self.timer.stop()
                self.tray.hide()
                QApplication.instance().quit()

    def _task_finished(self, task):
        if task.id in self._save_tasks and task.succeeded:
            key, body = self._save_tasks.pop(task.id)
            draft = self._drafts.get(key)
            version = task.results[-1].revision
            active = self.kb and str(self.kb) == key[0] and self.page and self.page.path == key[1]
            current_body = self.editor.toPlainText() if active else draft.body if draft else body
            confirmed = task.results[-1].page
            if version and (current_body != body or confirmed is None):
                self._drafts[key] = PageDraft(current_body, version)
            else:
                self._drafts.pop(key, None)
            if active and confirmed:
                # The receipt carries exactly the page this save committed.
                # A later filesystem read could silently adopt another writer's
                # unseen version and authorize overwriting it on the next save.
                self.page = confirmed
                if not self._quitting:
                    self.reader.show_markdown(
                        confirmed.body, (Path(key[0]) / "wiki" / key[1]).parent
                    )
                if current_body == body:
                    self.editor.blockSignals(True)
                    self.editor.setPlainText(confirmed.body)
                    self.editor.blockSignals(False)
        if task.id == self._chat_task and self.kb and str(self.kb) == task.kb_dir:
            message = task.text or task.error or "未保存回答正文；可从已保存的对话或产物查看。"
            if task.state != "completed":
                message += f"\n\n> {_STATES.get(task.state, task.state)}。未完成内容仅临时保留。"
            self.chat.show_markdown(message, self.kb / "wiki")
            if task.results and task.results[-1].session_id:
                session_id = task.results[-1].session_id
                self.sessions.addItem(session_id, session_id)
                self.sessions.setCurrentIndex(self.sessions.count() - 1)
        if self.kb and str(self.kb) == task.kb_dir and not self._quitting:
            self._refresh()
        self._select_task()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        event.ignore()
        if QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible():
            self.hide()
        else:
            self.statusBar().showMessage("系统托盘不可用，窗口保持可见。请使用“退出”结束程序。")

    def request_quit(self):
        if self._quitting:
            return
        stop = False
        if any(task.state not in TERMINAL for task in self.manager.tasks()):
            box = QMessageBox(self)
            box.setWindowTitle("退出 OpenKB")
            box.setText("后台仍有任务。选择等待完成，或在安全边界停止。已完成成果会保留。")
            wait = box.addButton("等待完成并退出", QMessageBox.ButtonRole.AcceptRole)
            halt = box.addButton("安全停止并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (wait, halt):
                return
            stop = box.clickedButton() == halt
        self._quitting = True
        self.setEnabled(False)
        self.manager.shutdown(stop=stop)
        self.io.stop()
        self.reader.stop_rendering()
        self.chat.stop_rendering()
        self.statusBar().showMessage("正在收尾并回收后台进程…")
