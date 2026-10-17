"""Explicit writer initialization when an existing KB has no public baseline."""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from ultrakb.application.knowledge_bases import open_kb
from ultrakb.kb_format import require_current_kb
from ultrakb.lifecycle import current_generation
from ultrakb.mutation import RecoveryRequired
from ultrakb.published_knowledge import PublicationUnavailable, current_pointer


def offer_baseline(window, root: Path, error, request_id: int, *, workspace=None) -> bool:
    """Offer initialization only for a supported KB with no public pointer."""
    pointer = current_pointer(root)
    if not isinstance(error, PublicationUnavailable) or pointer.exists() or pointer.is_symlink():
        return False
    try:
        require_current_kb(root)
        generation = current_generation(root)
    except (OSError, ValueError, RecoveryRequired):
        return False
    dialog = BaselineDialog(window, root, generation, request_id, workspace)
    window._publication_baseline_dialog = dialog
    dialog.show()
    return True


class BaselineDialog(QDialog):
    def __init__(self, window, root, generation, request_id, workspace):
        super().__init__(window)
        self.window = window
        self.root = root
        self.generation = generation
        self.request_id = request_id
        self.workspace = workspace
        self.cancelled = False
        self.setWindowTitle("建立公开阅读基线")
        self.resize(480, 180)
        layout = QVBoxLayout(self)
        self.status = QLabel("该知识库尚未建立公开阅读基线。建立后即可按固定版本阅读。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        location = QLabel(str(root))
        location.setWordWrap(True)
        layout.addWidget(location)
        buttons = QDialogButtonBox()
        self.initialize = buttons.addButton(
            "建立公开阅读基线", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.initialize.clicked.connect(self.start)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self.closed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_current)
        self.timer.start(100)

    def obsolete(self):
        if (
            self.cancelled
            or self.window._quitting
            or self.request_id != self.window._open_request_id
            or self.root in self.window._deleting_kbs
        ):
            return True
        try:
            return current_generation(self.root) != self.generation
        except (OSError, ValueError, RecoveryRequired):
            return True

    def check_current(self):
        if self.obsolete():
            self.reject()

    def closed(self, _result):
        self.cancelled = True
        self.timer.stop()
        if getattr(self.window, "_publication_baseline_dialog", None) is self:
            self.window._publication_baseline_dialog = None
        if (
            _result == QDialog.DialogCode.Rejected
            and self.request_id == self.window._open_request_id
        ):
            self.window.statusBar().showMessage("已取消建立公开阅读基线。")

    def start(self):
        if self.obsolete():
            self.reject()
            return
        self.initialize.setEnabled(False)
        self.status.setText("正在建立公开阅读基线…")

        def waiting():
            self.status.setText("正在等待知识库完成当前写入；可取消等待，稍后再建立基线。")

        def initialized(root, error):
            if self.obsolete():
                return
            if error:
                self.status.setText(f"建立失败：{error}")
                self.initialize.setEnabled(True)
                return
            self.accept()
            self.window.open_knowledge_base(root, workspace=self.workspace)

        self.window.io.submit(
            lambda: open_kb(self.root),
            initialized,
            kb=self.root,
            exclusive=True,
            global_settings=True,
            cancelled=self.obsolete,
            obsolete=self.obsolete,
            on_wait=waiting,
        )
