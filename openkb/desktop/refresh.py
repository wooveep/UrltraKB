"""Inspect refresh reasons, rebuild a selected view, and review protected changes."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.application.refresh import list_refresh_proposals, refresh_status
from openkb.application.views import list_views, view_scope
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import AcceptRefreshProposal, RefreshKnowledge


class RefreshDialog(QDialog):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed, self._task, self._generation = False, None, 0
        self.setWindowTitle("刷新知识与历史依据")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 740)
        layout = QVBoxLayout(self)
        self.views = QComboBox()
        self.views.setAccessibleName("要刷新的知识版本")
        self.views.currentIndexChanged.connect(self.reload)
        layout.addWidget(self.views)
        self.status = QLabel("正在读取知识版本…")
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

        def loaded(views, error):
            if error:
                self.status.setText(str(error))
                return
            self.views.blockSignals(True)
            for view in views:
                versions = ", ".join(view.applicable_versions) or "未指定版本"
                label = f"{view.product or '未指定产品'} / {versions}"
                if view.view_id == "legacy":
                    label = "旧知识库"
                self.views.addItem(label, view.view_id)
            current = self.views.findData(window.view_id)
            if current >= 0:
                self.views.setCurrentIndex(current)
            self.views.blockSignals(False)
            self.reload()

        window.io.submit(lambda: list_views(kb), loaded, kb=kb, obsolete=lambda: self._closed)

    def reload(self):
        view_id = self.views.currentData()
        if not view_id:
            return
        self._generation += 1
        generation = self._generation
        self.accept_button.setEnabled(False)
        self.proposals.clear()

        def read():
            scope = view_scope(self.kb, view_id)
            return refresh_status(self.kb, scope=scope), list_refresh_proposals(
                self.kb, scope=scope
            )

        def loaded(value, error):
            if error:
                self.status.setText(str(error))
                return
            status, proposals = value
            lines = []
            for page, reasons in status["needs_refresh"].items():
                lines.append(page)
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
            for proposal in proposals:
                if proposal["status"] != "awaiting_confirmation":
                    continue
                item = QListWidgetItem("人工修改需要审阅 · " + ", ".join(proposal["conflicts"]))
                item.setData(Qt.ItemDataRole.UserRole, proposal)
                self.proposals.addItem(item)
            self.run_button.setEnabled(self._task is None)

        self.window.io.submit(
            read,
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def show_history(self):
        from openkb.desktop.knowledge_history import KnowledgeHistoryDialog

        if self.views.currentData():
            KnowledgeHistoryDialog(self.window, self.kb, self.views.currentData()).show()

    def run(self):
        if self._task or not self.views.currentData():
            return
        self._task = self.window.manager.submit(
            self.kb, [RefreshKnowledge(view_id=self.views.currentData())]
        )
        self.run_button.setEnabled(False)
        self.status.setText("刷新任务已提交，可在主窗口查看进度或安全停止。")

    def selected(self, item, previous=None):
        proposal = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.diff.setPlainText(proposal["diff"] if proposal else "")
        self.accept_button.setEnabled(bool(proposal) and self._task is None)

    def accept_selected(self):
        item = self.proposals.currentItem()
        if item is None or self._task:
            return
        proposal = item.data(Qt.ItemDataRole.UserRole)
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
