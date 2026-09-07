"""Review a manual retry, or explicitly clear closed result summaries."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.runtime.task_actions import preview_retry, retry_task


class RetryDialog(QDialog):
    def __init__(self, window, task_id):
        super().__init__(window)
        self.window, self.task_id = window, task_id
        self.preview = None
        self._closed = False
        self.setWindowTitle("核对成果并重试")
        self.resize(840, 520)
        layout = QVBoxLayout(self)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        self.check_button = QPushButton("重新检查")
        self.check_button.clicked.connect(self.refresh)
        actions.addWidget(self.check_button)
        self.retry_button = QPushButton("创建新的重试任务")
        self.retry_button.clicked.connect(self.retry)
        self.retry_button.setEnabled(False)
        actions.addWidget(self.retry_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self):
        self.retry_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.details.setPlainText("正在核对结果凭据、已保留产物和知识库状态…")
        try:
            root = Path(self.window.manager.get(self.task_id).kb_dir)
        except KeyError:
            self.details.setPlainText("任务摘要已被清理。")
            return

        def loaded(preview, error):
            self.check_button.setEnabled(True)
            if error:
                self.details.setPlainText(str(error))
                return
            self.preview = preview
            lines = [preview.kb_dir, f"原任务：{preview.task_id}"]
            if preview.reason:
                lines.extend(["", preview.reason])
            else:
                numbers = "、".join(str(index + 1) for index in preview.indices)
                lines.extend(
                    [
                        "",
                        f"将重新执行第 {numbers} 项。已成功和已跳过项不会重复执行。",
                        "重试会创建独立的新任务，并在开始业务时读取当时的配置。",
                        "原请求的覆盖确认和版本条件会再次校验；过期时需要回到原操作重新确认。",
                    ]
                )
            lines.extend(f"\n仍存在的产物：{path}" for path in preview.retained)
            lines.extend(f"\n已移动或缺失的产物：{path}" for path in preview.missing)
            self.details.setPlainText("\n".join(lines))
            self.retry_button.setEnabled(preview.allowed)

        self.window.io.submit(
            lambda: preview_retry(self.window.manager, self.task_id),
            loaded,
            kb=root,
            obsolete=lambda: self._closed or self.window._quitting,
        )

    def retry(self):
        if self.preview is None or not self.preview.allowed:
            return
        preview = self.preview
        self.retry_button.setEnabled(False)
        self.check_button.setEnabled(False)

        def submitted(task_id, error):
            if error:
                self.details.appendPlainText(f"\n未创建任务：{error}")
                self.check_button.setEnabled(True)
                return
            self.details.appendPlainText(f"\n已创建新任务：{task_id}")
            self.check_button.setEnabled(True)

        self.window.io.submit(
            lambda: retry_task(self.window.manager, preview),
            submitted,
            kb=Path(preview.kb_dir),
            obsolete=lambda: self._closed or self.window._quitting,
        )

    def done(self, result):
        self._closed = True
        super().done(result)


def clear_selected_history(window, task_ids):
    if not task_ids:
        return
    selected = frozenset(task_ids)
    if any(selected & watch.referenced_task_ids() for watch in window.watch_registry.watches()):
        QMessageBox.information(window, "正在确认监听结果", "请等待监听确认这些任务结果后再清理。")
        return
    try:
        for task_id in selected:
            window.manager.retry_inputs(task_id)
    except (KeyError, ValueError) as exc:
        QMessageBox.information(window, "暂不能清理", str(exc))
        return
    if (
        QMessageBox.question(
            window,
            "清理任务摘要",
            f"清理所选 {len(selected)} 个已结束任务的摘要和结果凭据？知识库内容和产物会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        != QMessageBox.StandardButton.Yes
    ):
        return

    def completed(_value, error):
        if error:
            QMessageBox.warning(window, "摘要清理未完成", str(error))
        window._poll_tasks()

    window.io.submit(lambda: window.manager.clear_history(tuple(selected)), completed)
