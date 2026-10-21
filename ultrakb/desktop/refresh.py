"""Inspect refresh reasons, rebuild shared knowledge, and review protected changes."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ultrakb.application.refresh import (
    list_refresh_proposal_page,
    read_refresh_proposal,
    refresh_status,
)
from ultrakb.runtime.records import TERMINAL
from ultrakb.runtime.requests import AcceptRefreshProposal, RefreshKnowledge


class RefreshDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        captured = getattr(window, "read_scope", None)
        self.scope = (
            captured
            if captured is not None and captured.knowledge is not None
            else getattr(window, "scope", None)
        )
        self.view_id = self.scope.view_id if self.scope else None
        self._closed, self._task, self._generation = False, None, 0
        self._after, self._reviewed, self._selection = None, None, 0
        self._page_loading = False
        self.setWindowTitle("刷新知识与历史依据")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 740)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取知识库…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.run_button, self.reload_button = (
            QPushButton("使用当前有效来源刷新知识"),
            QPushButton("重新读取状态"),
        )
        self.run_button.clicked.connect(self.run)
        self.reload_button.clicked.connect(self.reload)
        actions.addWidget(self.run_button)
        actions.addWidget(self.reload_button)
        self.history_button = QPushButton("阅读历史知识")
        self.history_button.clicked.connect(self.show_history)
        actions.addWidget(self.history_button)
        layout.addLayout(actions)
        self.reasons = QPlainTextEdit()
        self.reasons.setReadOnly(True)
        self.reasons.setAccessibleName("待刷新页面与真实旧依据")
        layout.addWidget(self.reasons, 1)
        layout.addWidget(QLabel("需要审阅的刷新差异（请阅读全部差异后接受）"))
        self.proposals = QListWidget()
        self.proposals.currentItemChanged.connect(self.selected)
        layout.addWidget(self.proposals)
        self.more_button = QPushButton("加载更多差异")
        self.more_button.setEnabled(False)
        self.more_button.clicked.connect(self.load_more)
        layout.addWidget(self.more_button)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        layout.addWidget(self.diff, 1)
        self.accept_button = QPushButton("接受所示全部差异")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept_selected)
        layout.addWidget(self.accept_button)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)

        self.reload()

    def reload(self):
        self._generation += 1
        generation = self._generation
        self.accept_button.setEnabled(False)
        self.more_button.setEnabled(False)
        self._reviewed, self._after, self._page_loading = None, None, True
        self.proposals.clear()
        self.diff.clear()
        selected = self.scope

        def read():
            scope = selected
            if scope is not None and scope.knowledge is not None:
                from ultrakb.application.views import storage_scope

                scope = storage_scope(self.kb, scope.knowledge.identity, view_id=scope.view_id)
            return (
                scope,
                refresh_status(self.kb, scope=scope),
                list_refresh_proposal_page(self.kb, scope=scope),
            )

        def loaded(value, error):
            self._page_loading = False
            if error:
                self.status.setText(str(error))
                return
            self.scope, status, page = value
            lines = []
            for page_path, reasons in status["needs_refresh"].items():
                lines.append(page_path)
                for reason in reasons:
                    label = {"updated": "来源更新", "empty": "确认清空", "withdrawn": "来源撤回"}[
                        reason["kind"]
                    ]
                    lines.append(f"  {label} · 实际旧依据 {reason['source_revision_id']}")
            self.reasons.setPlainText("\n".join(lines) or "没有待刷新的页面。")
            self.status.setText(
                f"{len(status['needs_refresh'])} 个页面待刷新；"
                f"{len(status['effective_inputs'])} 个当前有效输入。旧知识和历史原文将保留。"
            )
            self.append_proposals(page)
            self.run_button.setEnabled(self._task is None)

        self._read(
            read,
            loaded,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def _read(self, operation, callback, *, obsolete):
        scope = self.scope
        if scope is None or scope.knowledge is None:
            self.window.io.submit(operation, callback, kb=self.kb, obsolete=obsolete)
            return
        from ultrakb.application.views import run_scoped_operation

        def read():
            return run_scoped_operation(operation, scope=scope, cancelled=obsolete)

        self.window.io.submit(read, callback, obsolete=obsolete)

    def append_proposals(self, page):
        for proposal in page.proposals:
            if proposal["status"] != "awaiting_confirmation":
                continue
            count = proposal.get("conflict_count", len(proposal.get("conflicts", ())))
            item = QListWidgetItem(f"人工修改需要审阅 · {count} 个页面")
            item.setData(Qt.ItemDataRole.UserRole, proposal)
            self.proposals.addItem(item)
        self._after = page.next_after
        self.more_button.setEnabled(self._after is not None)

    def load_more(self):
        if self._page_loading or self._after is None:
            return
        generation, after, scope = self._generation, self._after, self.scope
        self._page_loading = True
        self.more_button.setEnabled(False)

        def loaded(page, error):
            self._page_loading = False
            if error:
                self.status.setText(str(error))
                self.more_button.setEnabled(True)
                return
            self.append_proposals(page)

        self._read(
            lambda: list_refresh_proposal_page(self.kb, scope=scope, after=after),
            loaded,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def show_history(self):
        from ultrakb.desktop.knowledge_history import KnowledgeHistoryDialog

        KnowledgeHistoryDialog(self.window, self.kb, self.view_id or "legacy").show()

    def run(self):
        if self._task:
            return
        from ultrakb.desktop.model_actions import submit_panel

        def submitted(task):
            self._task = task
            self.run_button.setEnabled(False)
            self.status.setText("刷新任务已提交，可在主窗口查看进度或安全停止。")

        submit_panel(
            self,
            [RefreshKnowledge(view_id=self.view_id)],
            description="更新知识",
            on_submitted=submitted,
        )

    def selected(self, item, previous=None):
        self._selection += 1
        selection = self._selection
        self._reviewed = None
        self.accept_button.setEnabled(False)
        self.diff.clear()
        proposal = item.data(Qt.ItemDataRole.UserRole) if item else None
        if proposal is None:
            return
        scope = self.scope

        def loaded(value, error):
            if error:
                self.status.setText(str(error))
                return
            self._reviewed = value
            self.diff.setPlainText(value["diff"])
            self.accept_button.setEnabled(
                value["status"] == "awaiting_confirmation" and self._task is None
            )

        self._read(
            lambda: read_refresh_proposal(self.kb, proposal["proposal_id"], scope=scope),
            loaded,
            obsolete=lambda: self._closed or selection != self._selection,
        )

    def accept_selected(self):
        item = self.proposals.currentItem()
        if item is None or self._task or self._reviewed is None:
            return
        proposal = self._reviewed
        self._task = self.window.manager.submit(
            self.kb,
            [
                AcceptRefreshProposal(
                    proposal["proposal_id"], proposal["version"], view_id=proposal["view_id"]
                )
            ],
        )
        self.accept_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.status.setText("正在核对来源和页面后接受差异…")

    def poll(self):
        if self._task is None:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        self.reload()
        self.status.setText("\n".join(result.error or result.status for result in task.results))

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)

    def closeEvent(self, event):
        self._closed = True
        self.timer.stop()
        super().closeEvent(event)
