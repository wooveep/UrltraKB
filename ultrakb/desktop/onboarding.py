"""Keep one immutable user action while native connection setup is pending."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ultrakb.application.model_connections import read_connection_state
from ultrakb.desktop.setup_wizard import SetupWizard
from ultrakb.lifecycle import current_generation


@dataclass(frozen=True)
class PendingAction:
    root: Path
    generation: str
    description: str
    continue_action: Callable = field(repr=False)
    valid: Callable = field(default=lambda: True, repr=False)


class ConnectionGuide(SetupWizard):
    def __init__(self, guide, pending):
        super().__init__(guide.window, kb=pending.root)
        self.guide, self.pending = guide, pending
        self.setWindowTitle("配置模型连接并继续")
        self.description.setText(
            f"知识库：{pending.root.name} · 完成后{pending.description}。"
            "稍后继续会保留原输入和已保存的设置。"
        )

    def connection_verified(self, state):
        self.guide.completed(self)

    def done(self, result):
        super().done(result)
        if self.guide.dialog is self:
            self.guide.dialog = None
            self.guide._paused = True
            self.guide._generation += 1
            self.guide.update_overview()


class Onboarding:
    def __init__(self, window):
        self.window = window
        self.pending = None
        self.dialog = None
        self.ready = {}
        self._checking = set()
        self._generation = 0
        self._paused = True

    def switched(self, root):
        self._generation += 1
        self._paused = True
        if self.dialog:
            self.dialog.reject()
        self.update_overview()
        self.refresh(root)

    def invalidate(self, root):
        if root is None:
            self.ready.clear()
        else:
            self.ready.pop(root, None)
        self.refresh(self.window.kb)

    def refresh(self, root):
        if root is None or root in self._checking:
            return
        self._checking.add(root)

        def loaded(state, error):
            self._checking.discard(root)
            if not error:
                self.ready[root] = state.state == "passed"
            self.update_overview()

        self.window.io.submit(
            lambda: read_connection_state(root),
            loaded,
            kb=root,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.window._quitting,
        )

    def update_overview(self):
        w = self.window
        label = getattr(w.workspaces, "setup_status", None)
        if label is None:
            return
        pending = self.pending if self.pending and self.pending.root == w.kb else None
        label.setText(
            f"待继续：{pending.description}。目标：{pending.root.name}。"
            if pending
            else "模型连接已就绪，可以导入资料或提问。"
            if self.ready.get(w.kb)
            else "下一步：连接模型服务，再导入第一份资料。"
        )
        w.workspaces.setup_continue.setEnabled(w.kb is not None)
        w.workspaces.setup_continue.setVisible(bool(pending) or not self.ready.get(w.kb))
        w.workspaces.setup_continue.setText("继续原操作" if pending else "连接模型服务")

    def ensure(self, root, description, continue_action, *, valid=lambda: True):
        root = root.expanduser().resolve()
        if self.window._quitting:
            return
        if self.pending is not None and not self._paused:
            if self.dialog:
                self.dialog.raise_()
            self.window.statusBar().showMessage("已有待继续操作；请完成设置或选择稍后继续。")
            return
        action = PendingAction(root, current_generation(root), description, continue_action, valid)
        if root != self.window.kb:
            self.pending, self._paused = action, True
            self.window.statusBar().showMessage("已切换知识库；原输入保留，请回原知识库继续操作。")
            self.update_overview()
            return
        if not valid():
            return
        # A verified UI can queue immediately while a KB writer owns its lease.
        # Settings saves invalidate this cache; execution independently checks
        # its captured configuration after acquiring the actual execution lease.
        if self.ready.get(root):
            self._execute(action)
            return
        self.pending = action
        self._paused = False
        generation = self._generation

        def loaded(state, error):
            if self.pending is not action or generation != self._generation:
                return
            if error:
                self._paused = True
                self.window.statusBar().showMessage(
                    "无法检查连接；原操作和输入已保留，可从概览继续。"
                )
            elif state.state == "passed":
                self.ready[root] = True
                self.pending = None
                self._execute(action)
            else:
                self.open(action)
            self.update_overview()

        self.window.io.submit(
            lambda: read_connection_state(root),
            loaded,
            kb=root,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.window._quitting,
        )

    def submit(
        self,
        root,
        requests,
        *,
        on_submitted=lambda _task: None,
        description=None,
        valid=lambda: True,
    ):
        units = tuple(requests)

        def submit():
            from ultrakb.runtime.requests import ImportFile, ImportUrl

            task = self.window.manager.submit(root, units, require_verified_connection=True)
            if units and isinstance(units[0], (ImportFile, ImportUrl)):
                self.window.workspaces.import_progress.track(root, task, view_id=units[0].view_id)
            on_submitted(task)

        titles = {
            "ImportFile": "导入资料",
            "ImportUrl": "导入网址",
            "AskQuestion": "提问",
            "ContinueConversation": "继续提问",
            "RecompileDocument": "重新生成知识",
            "RetrySource": "重试未完成资料",
            "ReprocessSource": "重新处理资料",
            "GenerateArtifact": "生成产物",
            "CheckKnowledge": "检查知识",
            "RefreshKnowledge": "更新知识",
        }
        self.ensure(
            root,
            description or f"{titles.get(type(units[0]).__name__, '继续操作')} · {len(units)} 项",
            submit,
            valid=valid,
        )

    def _execute(self, action):
        if (
            self.window._quitting
            or self.window.kb != action.root
            or action.root in self.window._deleting_kbs
            or not action.valid()
        ):
            self.window.statusBar().showMessage("原操作界面已关闭或目标已改变，请重新选择操作。")
            return
        try:
            if current_generation(action.root) != action.generation:
                raise ValueError("知识库已变更，请重新选择原操作。")
            action.continue_action()
        except Exception as error:
            self.window._error(error)

    def open(self, action):
        if self.dialog:
            self.dialog.raise_()
            return
        self.dialog = ConnectionGuide(self, action)
        self.dialog.finished.connect(self.dialog.deleteLater)
        self._paused = False
        self.dialog.show()
        self.update_overview()

    def resume(self):
        root = self.window.kb
        if root is None:
            return
        action = (
            self.pending
            if self.pending and self.pending.root == root
            else PendingAction(
                root,
                current_generation(root),
                "完成连接配置（不会自动导入或提问）",
                lambda: None,
            )
        )
        self.pending = action
        self.open(action)

    def completed(self, dialog):
        action = dialog.pending
        if self.dialog is not dialog or self.pending is not action:
            return
        generation = self._generation

        def checked(state, error):
            if (
                self.dialog is not dialog
                or self.pending is not action
                or generation != self._generation
            ):
                return
            if error or state.state != "passed":
                dialog.connection.state = None
                dialog.connection.status.setText("当前连接尚未通过验证或已变更，请重新测试后继续。")
                dialog.update_controls()
                return
            self.ready[action.root] = True
            self.pending = None
            dialog.accept()
            self._execute(action)
            self.update_overview()

        self.window.io.submit(
            lambda: read_connection_state(action.root),
            checked,
            kb=action.root,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.window._quitting or self.dialog is not dialog,
        )
