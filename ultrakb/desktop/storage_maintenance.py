"""Native explicit storage maintenance, with I/O outside the Qt thread."""

from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ultrakb.application.storage_maintenance import (
    MaintenanceOverview,
    backup_knowledge,
    collect_garbage,
    preview_storage_maintenance,
    repair_knowledge_receipts,
    restore_knowledge,
    retire_unused_roots,
)
from ultrakb.desktop.io import LocalIO
from ultrakb.storage.database import DatabaseIdentity


def _result_text(result):
    values = asdict(result)
    labels = {
        "retired_checkpoints": "已释放会话检查点",
        "retired_plans": "已释放同步计划明细",
        "retired_sync_items": "已释放同步条目",
        "released_reference_bytes": "已释放引用载荷字节（可重复引用同一内容）",
        "deleted_count": "已删除孤立内容",
        "deleted_bytes": "已删除内容字节",
        "trees_deleted": "已删除无引用检索树",
        "tree_rows_deleted": "已删除检索树条目",
        "repaired": "已修复回执",
        "invalid": "仍需检查的回执",
        "remaining": "剩余回执",
        "path": "保存目录",
    }
    lines = [f"{label}：{values[key]}" for key, label in labels.items() if key in values]
    if values.get("has_more"):
        lines.append("仍有候选，可继续执行下一批。")
    if "coverage" in values:
        labels = {
            "committed_database": "已提交数据",
            "referenced_blobs": "所引用内容",
            "configuration": "本库设置",
            "inherited_configuration": "继承的全局设置",
            "worktree": "暂停中的编辑差异",
            "external_receipts": "待确认回执",
        }
        states = {"complete": "完整", "absent": "未设置", "partial": "部分", "external": "库外"}
        lines.extend(
            f"{labels.get(key, key)}：{states.get(value, value)}"
            for key, value in values["coverage"].items()
        )
    return "\n".join(lines)


class StorageMaintenanceDialog(QDialog):
    def __init__(self, kb_dir, parent=None):
        super().__init__(parent)
        self.root = Path(kb_dir).resolve()
        self.identity = self.overview = self.last_result = None
        self.busy, self._mutating, self._closed = False, False, False
        self.io = LocalIO(self)
        self.setWindowTitle(f"存储维护 · {self.root.name}")
        self.resize(920, 640)
        layout = QVBoxLayout(self)
        location = QLabel(str(self.root))
        location.setTextFormat(Qt.TextFormat.PlainText)
        location.setWordWrap(True)
        layout.addWidget(location)
        self.status = QLabel("正在预览…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        layout.addWidget(self.body)
        controls = QGridLayout()
        self.buttons = []
        for index, (label, callback) in enumerate(
            (
                ("刷新预览", self.refresh),
                ("释放无引用恢复材料", self.retire_roots),
                ("清理孤立内容", self.collect),
                ("修复待确认回执", self.repair_receipts),
                ("备份到新目录…", self.choose_backup),
                ("从备份恢复到新目录…", self.choose_restore),
            )
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button, index // 3, index % 3)
            self.buttons.append(button)
        layout.addLayout(controls)
        self.refresh()

    def _submit(self, operation, *, mutating=False):
        if self.busy or self._closed:
            return
        self.busy, self._mutating = True, mutating
        for button in self.buttons:
            button.setEnabled(False)
        self.status.setText("正在执行本批维护…" if mutating else "正在读取维护预览…")

        def loaded(value, error):
            self.busy = self._mutating = False
            for button in self.buttons:
                button.setEnabled(
                    self.identity is not None or button in (self.buttons[0], self.buttons[-1])
                )
            if error:
                if mutating:
                    self._display(operation_error=error)
                else:
                    self._display(preview_error=error)
                return
            if isinstance(value, MaintenanceOverview):
                self.overview = value
                self.identity = DatabaseIdentity(value.kb_id, value.generation)
                for button in self.buttons:
                    button.setEnabled(True)
                self._display()
            else:
                self.last_result = value
                self._display()
                self.refresh()

        # The use case owns its lifecycle/reclamation protection. An outer
        # legacy kb_lock would obstruct reclamation and reject an unpublished KB.
        self.io.submit(operation, loaded, obsolete=lambda: self._closed)

    def _display(self, *, preview_error=None, operation_error=None):
        overview = self.overview
        lines = []
        if overview is not None:
            roots, garbage = overview.roots, overview.garbage
            reason = roots.blocked_reason or garbage.blocked_reason
            self.status.setText(
                "存在归属尚未确认的回执，暂不能清理。"
                if reason
                else "预览已更新，执行时会重新核对当前引用。"
            )
            lines = [
                f"可释放恢复材料：检查点 {roots.candidate_checkpoints}，"
                f"同步计划 {roots.candidate_plans}。",
                f"受保护恢复材料：检查点 {roots.protected_checkpoints}，"
                f"同步计划 {roots.protected_plans}。",
                f"候选引用载荷：{roots.candidate_reference_bytes} 字节（不是磁盘回收量）。",
                f"可回收孤立内容：{garbage.candidate_count} 项，{garbage.candidate_bytes} 字节；"
                f"检索树 {garbage.candidate_trees}。",
                f"受保护内容：{garbage.protected_count} 项，{garbage.protected_bytes} 字节。",
                "预览和执行均有批次上限；可继续刷新并处理下一批。",
                f"统计时间：{overview.statistics_at}",
            ]
        if self.last_result is not None:
            state = getattr(self.last_result, "status", "completed")
            states = {
                "completed": "已完成本批",
                "busy": "存储正在使用中，本批未清理",
                "blocked": "本批受阻",
            }
            lines.extend(["", states.get(state, state), _result_text(self.last_result)])
        if preview_error is not None:
            # A read failure does not undo a completed backup, restore or collection.
            self.status.setText(f"预览未更新：{preview_error}")
            lines.extend(["", f"预览未更新：{preview_error}"])
        if operation_error is not None:
            self.status.setText(f"本批维护未完成：{operation_error}")
            lines.extend(["", f"本批维护未完成：{operation_error}"])
        self.body.setPlainText("\n".join(lines))

    def refresh(self):
        self._submit(
            lambda: preview_storage_maintenance(
                self.root, identity=self.identity, cancelled=lambda: self._closed
            )
        )

    def _maintain(self, operation):
        if self.identity is None or self.busy or self._closed:
            return
        self.last_result = None
        self._submit(lambda: operation(self.root, identity=self.identity), mutating=True)

    def retire_roots(self):
        self._maintain(retire_unused_roots)

    def collect(self):
        self._maintain(collect_garbage)

    def repair_receipts(self):
        self._maintain(repair_knowledge_receipts)

    def choose_backup(self):
        destination, _ = QFileDialog.getSaveFileName(self, "选择新备份目录", "ultrakb-backup")
        if destination:
            self.backup_to(Path(destination))

    def backup_to(self, destination):
        if self.identity is None or self.busy or self._closed:
            return
        self.last_result = None
        self._submit(
            lambda: backup_knowledge(self.root, Path(destination), identity=self.identity),
            mutating=True,
        )

    def choose_restore(self):
        source = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if not source:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "选择新恢复目录", "ultrakb-restored")
        if destination:
            self.restore_to(Path(source), Path(destination))

    def restore_to(self, backup, destination):
        if self.busy or self._closed:
            return
        self.last_result = None
        self._submit(lambda: restore_knowledge(Path(backup), Path(destination)), mutating=True)

    def done(self, result):
        if self.busy and self._mutating:
            self.status.setText("本批维护仍在访问存储，结束后可关闭。")
            return
        self._closed = True
        self.io.stop()
        super().done(result)
