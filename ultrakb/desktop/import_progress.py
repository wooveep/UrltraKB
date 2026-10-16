"""Keep import progress and next steps attached to the submitted KB and view."""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ultrakb.desktop.flow_layout import FlowLayout
from ultrakb.runtime.records import TERMINAL


class ImportProgress(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.task_id = self.root = self.view_id = None
        self._checked = self._terminal = None
        self._available = None
        self._generation = 0
        self._question_request = None
        self._observed = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("导入处理进度")
        layout.addWidget(self.progress)
        row = FlowLayout()
        self.details = QPushButton("查看导入任务详情")
        self.details.clicked.connect(self.show_details)
        row.addWidget(self.details)
        self.ask = QPushButton("开始提问")
        self.ask.clicked.connect(self.begin_question)
        row.addWidget(self.ask)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.hide()

    def track(self, root, task_id, *, view_id=None):
        self.root, self.task_id, self.view_id = Path(root), task_id, view_id or "legacy"
        self._checked = self._terminal = None
        self._available = None
        self._generation += 1
        self._question_request = None
        self.ask.setEnabled(False)
        self.show()
        self.poll()

    def poll(self):
        if self.window._quitting:
            self.timer.stop()
            return
        if self.task_id is None:
            return
        try:
            task = self.window.manager.get(self.task_id)
        except KeyError:
            self.hide()
            return
        from ultrakb.desktop.task_status import current_source, task_counts, task_progress

        self.progress.setRange(0, task.total)
        self.progress.setValue(len(task.results))
        self.progress.setFormat("%v / %m 项已处理")

        target = f"导入目标：{self.root}"
        if self.window.kb != self.root:
            target += "（当前正在浏览其他知识库）"
        if task.state not in TERMINAL:
            source = current_source(task)
            self.status.setText(
                f"{target}\n{task_counts(task)}\n"
                + (f"当前资料：{source} · " if source else "")
                + task_progress(task)
            )
            self.ask.setEnabled(False)
            return
        partial = task.state != "completed" or any(
            result.unfinished or result.quality for result in task.results
        )
        self.status.setText(
            f"{target}\n{task_counts(task)}。"
            + ("未完成范围可在任务详情核对。" if partial else "导入完成。")
            + (
                " 尚无可用于提问的内容，请查看任务详情或在资料行“更多”中恢复未完成项。"
                if self._available is False
                else ""
            )
        )
        self.ask.setText("开始提问" if self.window.kb == self.root else "切回导入目标并提问")
        self.ask.setToolTip(str(self.root))
        if self._terminal != task.state:
            self._terminal = task.state
            self.window.statusBar().showMessage(self.status.text(), 15000)
        if self._checked != task.state:
            self._checked = task.state
            self.check_available()

    def check_available(self, *, continue_action=False):
        from ultrakb.application.document_actions import document_inventory
        from ultrakb.knowledge_scope import live_scope

        generation = self._generation
        request = self.window._open_request_id
        root, view_id = self.root, self.view_id
        if continue_action:
            self._question_request = (request, object())
        question_request = self._question_request
        self.ask.setEnabled(False)

        def loaded(rows, error):
            available = not error and any(row.actions["ask"].allowed for row in rows)
            self._available = bool(available)
            self.ask.setEnabled(bool(available))
            if not available:
                if question_request == self._question_request:
                    self._question_request = None
                self.status.setText(
                    f"{root.name}：尚无可用于提问的内容。请查看导入任务详情，"
                    "在资料行“更多”中恢复未完成项。"
                )
            elif question_request is not None and question_request == self._question_request:
                # A refresh replaces the read, not the user's still-current
                # click. Older reads cannot consume a newer click's intent.
                self._question_request = None
                if question_request[0] == self.window._open_request_id:
                    self.open_target(root, view_id)

        self.window.io.submit(
            lambda: document_inventory(root, scope=live_scope(root, view_id)),
            loaded,
            kb=root,
            obsolete=lambda: self.window._quitting
            or generation != self._generation
            or (continue_action and request != self.window._open_request_id),
        )

    def begin_question(self):
        self.check_available(continue_action=True)

    def refresh_available(self, root):
        if self.root == root and self._terminal is not None:
            self._generation += 1
            self.check_available()

    def observe(self, task):
        """Refresh committed files between import units, without polling KB storage."""
        if task.operation not in {"ImportFile", "ImportUrl"}:
            return
        count = len(task.results)
        previous = self._observed.get(task.id, 0)
        self._observed[task.id] = count
        if count > previous and str(self.window.kb) == task.kb_dir:
            self.window.workspaces.refresh_inventory(task)

    def open_target(self, root, view_id):
        window = self.window
        if window.kb != root:
            window.open_knowledge_base(root)
        request = window._open_request_id

        def ready():
            if window._quitting or request != window._open_request_id:
                return
            if window._pending_open is not None:
                QTimer.singleShot(50, ready)
            elif window.kb == root:
                if (window.view_id or "legacy") != view_id:
                    window.statusBar().showMessage("请先选择该导入任务所属的知识视图，再开始提问。")
                    return
                window.shell.navigate("对话")
                if window.conversations.active is None:
                    window.conversations.new()
                window.question.setFocus()

        ready()

    def show_details(self):
        from ultrakb.desktop.task_details import show_task_details

        try:
            task = self.window.manager.get(self.task_id)
        except KeyError:
            self.hide()
            return
        show_task_details(task, self.window)
