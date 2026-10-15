"""Explicit task batches review retained facts and skip inapplicable identities."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ultrakb.runtime.task_actions import preview_retry, retry_task


class RetryDialog(QDialog):
    def __init__(self, window, task_ids):
        super().__init__(window)
        self.window = window
        self.task_ids = tuple(dict.fromkeys((task_ids,) if isinstance(task_ids, str) else task_ids))
        self.task_id = self.task_ids[0] if self.task_ids else None
        self.preview = None
        self.previews, self.reasons = {}, {}
        self._closed = False
        self._refresh_id = 0
        self._run_id = 0
        self.setWindowTitle("核对成果并重试")
        from ultrakb.desktop.dialog_geometry import resize_to_screen

        resize_to_screen(self, 840, 560)
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
        self._refresh_id += 1
        self._run_id += 1
        generation = self._refresh_id
        self.retry_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.previews, self.reasons = {}, {}
        self._remaining = set(self.task_ids)
        self.details.setPlainText("正在逐项核对结果凭据、保留产物和知识库状态…")
        for task_id in self.task_ids:
            try:
                view, _requests, _identities = self.window.manager.retry_inputs(task_id)
            except (KeyError, ValueError) as error:
                self.reasons[task_id] = str(error) or "任务摘要已清理。"
                self._remaining.discard(task_id)
                continue

            def loaded(preview, error, identity=task_id):
                self._remaining.discard(identity)
                if error:
                    self.reasons[identity] = str(error)
                elif not preview.allowed:
                    self.reasons[identity] = preview.reason or "没有可重试的未完成项。"
                else:
                    self.previews[identity] = preview
                self.render_preview()

            self.window.io.submit(
                lambda identity=task_id: preview_retry(self.window.manager, identity),
                loaded,
                kb=Path(view.kb_dir),
                obsolete=lambda: self._closed
                or self.window._quitting
                or generation != self._refresh_id,
                on_wait=lambda: self.details.appendPlainText(
                    "正在等待所属知识库完成当前写入后核对重试范围…"
                ),
            )
        self.render_preview()

    def render_preview(self):
        self.preview = self.previews.get(self.task_id)
        lines = [
            f"选中 {len(self.task_ids)} 项 · 可重试 {len(self.previews)} 项 · "
            f"不执行 {len(self.reasons)} 项 · 正在核对 {len(self._remaining)} 项。",
            "每项创建独立的新任务，仅重试未完成单元；成功和跳过项不会重复执行。",
            "实际配置、覆盖确认和版本条件会再次检查；关闭窗口不会发起后续重试。",
            "重新检查会取消尚未提交的后续重试，已提交任务在任务列表中保留。",
        ]
        for identity in self.task_ids:
            if identity in self.previews:
                preview = self.previews[identity]
                numbers = "、".join(str(index + 1) for index in preview.indices)
                lines.extend(
                    ["", f"适用：{preview.kb_dir}\n任务：{identity}\n仅重试第 {numbers} 项。"]
                )
                lines.extend(f"保留产物：{path}" for path in preview.retained)
                lines.extend(f"已移动或缺失的产物：{path}" for path in preview.missing)
            elif identity in self.reasons:
                lines.extend(["", f"不执行：{identity} · {self.reasons[identity]}"])
        self.details.setPlainText("\n".join(lines))
        self.check_button.setEnabled(not self._remaining)
        self.retry_button.setEnabled(bool(self.previews) and not self._remaining)

    def retry(self):
        if self._remaining or not self.previews or not self.retry_button.isEnabled():
            return
        previews = tuple(
            self.previews[identity] for identity in self.task_ids if identity in self.previews
        )
        run_id = self._run_id
        self.retry_button.setEnabled(False)
        self.check_button.setEnabled(False)

        def next_item(index):
            if self._closed or self.window._quitting or run_id != self._run_id:
                return
            if index == len(previews):
                self.check_button.setEnabled(True)
                self.window._poll_tasks()
                return
            preview = previews[index]

            def submitted(task_id, error):
                self.details.appendPlainText(
                    f"\n未创建 {preview.task_id}：{error}"
                    if error
                    else f"\n已创建新任务：{task_id}（原任务 {preview.task_id}）"
                )
                next_item(index + 1)

            def create_retry():
                if self._closed or run_id != self._run_id:
                    return
                self.check_button.setEnabled(False)
                self.window.io.submit(
                    lambda: retry_task(self.window.manager, preview),
                    submitted,
                    kb=Path(preview.kb_dir),
                    obsolete=lambda: self._closed
                    or self.window._quitting
                    or run_id != self._run_id,
                    on_wait=lambda: self.details.appendPlainText(
                        "正在等待知识库，随后再次校验并提交…"
                    ),
                )

            from ultrakb.runtime.requests import requires_model

            try:
                _view, requests, _identities = self.window.manager.retry_inputs(preview.task_id)
                model_required = any(requires_model(requests[i]) for i in preview.indices)
            except (KeyError, ValueError, IndexError) as error:
                submitted(None, error)
                return
            if model_required:
                self.check_button.setEnabled(True)
                self.window.onboarding.ensure(
                    Path(preview.kb_dir),
                    "重试未完成任务",
                    create_retry,
                    valid=lambda: not self._closed and run_id == self._run_id,
                    background=True,
                )
            else:
                create_retry()

        next_item(0)

    def done(self, result):
        self._closed = True
        self._refresh_id += 1
        super().done(result)


def clear_selected_history(window, task_ids):
    selected = tuple(dict.fromkeys(task_ids))
    if not selected:
        return
    referenced = set().union(
        *(watch.referenced_task_ids() for watch in window.watch_registry.watches())
    )
    eligible, skipped, lines = [], [], []
    for identity in selected:
        try:
            if identity in referenced:
                raise ValueError("目录监听仍在确认该任务结果，请稍后清理。")
            view, _requests, _identities = window.manager.retry_inputs(identity)
            eligible.append(identity)
            lines.append(f"适用：{view.kb_dir}\n{identity}")
        except (KeyError, ValueError) as error:
            skipped.append(identity)
            lines.append(f"不执行：{identity} · {error}")
    if not eligible:
        QMessageBox.information(window, "暂不能清理", "\n".join(lines))
        return
    question = QMessageBox(window)
    question.setWindowTitle("清理任务摘要")
    question.setText(
        f"选中 {len(selected)} 项，仅清理 {len(eligible)} 项；{len(skipped)} 项不执行。"
    )
    question.setInformativeText(
        "只清理已结束任务的摘要和结果凭据；知识库内容、原始资料及产物保留。"
    )
    question.setDetailedText("\n".join(lines))
    question.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    question.setDefaultButton(QMessageBox.StandardButton.No)
    if question.exec() != QMessageBox.StandardButton.Yes:
        return
    # Recheck watch ownership after the confirmation; manager rechecks active handles as well.
    if any(
        set(eligible) & watch.referenced_task_ids() for watch in window.watch_registry.watches()
    ):
        QMessageBox.information(window, "暂不能清理", "监听引用已变化，请重新核对清理范围。")
        return

    def completed(_value, error):
        if error:
            QMessageBox.warning(window, "摘要清理未完成", str(error))
        window._poll_tasks()
        window.workspaces.task_workspace.status.setText(
            f"已清理 {len(eligible)} 项任务摘要；跳过 {len(skipped)} 项。"
            if not error
            else "清理未全部完成，请核对当前列表。"
        )

    window.io.submit(lambda: window.manager.clear_history(tuple(eligible)), completed)
