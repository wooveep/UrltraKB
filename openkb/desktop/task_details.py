"""Task evidence remains separate from temporary model output."""

from datetime import datetime, timezone

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from openkb.desktop.task_progress import progress_presentation, update_progress_bar
from openkb.runtime.records import TERMINAL


def _status_text(task) -> str:
    _, progress_text, progress_detail = progress_presentation(task)
    lines = [
        task.kb_dir,
        f"任务：{task.id}",
        f"状态：{task.state}",
        f"阶段：{task.stage}",
        f"进度：{progress_text}",
        progress_detail,
        f"成功：{task.succeeded} · 跳过：{task.skipped} · "
        f"失败：{task.failed} · 未处理：{task.unfinished}",
        f"错误：{task.error or '无'}",
        f"已请求安全停止：{task.stop_requested}",
        f"进程已回收：{task.processes_reaped}",
    ]
    if task.started_at:
        timing = f"开始时间：{task.started_at}"
        if task.state not in TERMINAL:
            try:
                began = datetime.fromisoformat(task.started_at)
                elapsed = (datetime.now(timezone.utc) - began).total_seconds()
                timing += f" · 已用时 {max(0, int(elapsed))} 秒"
            except (ValueError, TypeError):
                pass
        lines.append(timing)
    if task.last_activity_at:
        lines.append(f"最近进度输出：{task.last_activity_at}（存活提示不代表业务进度）")
    if task.retry_of:
        lines.append(f"重试来源：{task.retry_of}（独立的新任务）")
    for number, result in enumerate(task.results, 1):
        lines.extend(["", f"第 {number} 项：{result.status}"])
        if result.error:
            lines.append(result.error)
        lines.extend(result.changes)
        lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        lines.extend(f"辅助告警：{warning}" for warning in result.warnings)
        if result.document:
            document = result.document
            lines.extend(
                [
                    f"原文接入：{document.source_intake}",
                    f"知识编译：{document.knowledge_compilation}",
                    f"处理阶段：{document.stage}",
                ]
            )
            if document.reason:
                lines.append(f"未完成原因：{document.reason}")
            lines.extend(f"辅助告警：{warning}" for warning in document.warnings)
            if document.usage:
                usage = document.usage
                lines.append(
                    f"模型尝试：{usage['observable_attempts']}，"
                    f"计入预算 token：{usage['charged_tokens']}，"
                    f"用量未知请求：{usage['unknown_usage']}"
                )
        lines.extend(f"质量检查：{note}" for note in result.quality)
        lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
    return "\n".join(lines)


def show_task_details(task, parent, *, manager=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("任务进度与日志")
    dialog.resize(900, 680)
    layout = QVBoxLayout(dialog)
    progress_bar = QProgressBar()
    progress_bar.setObjectName("task-detail-progress")
    progress_label = QLabel()
    progress_label.setWordWrap(True)
    layout.addWidget(progress_bar)
    layout.addWidget(progress_label)
    tabs = QTabWidget(dialog)
    status, logs = QPlainTextEdit(), QPlainTextEdit()
    status.setObjectName("task-status")
    logs.setObjectName("task-log")
    status.setReadOnly(True)
    logs.setReadOnly(True)
    tabs.addTab(status, "任务状态")
    tabs.addTab(logs, "运行日志")
    layout.addWidget(tabs)
    directory = manager.history_dir / "logs" / task.id if manager else None
    if directory is not None:
        button = QPushButton("打开日志目录")
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))))
        layout.addWidget(button)

    def refresh():
        current = task
        if manager is not None:
            try:
                current = manager.get(task.id)
            except KeyError:
                return
        status_text = _status_text(current)
        update_progress_bar(progress_bar, current)
        _, title, detail = progress_presentation(current)
        progress_label.setText(title + "\n" + detail)
        if status.toPlainText() != status_text:
            status.setPlainText(status_text)
        content = current.diagnostics
        if not content and directory is not None:
            from openkb.runtime.diagnostics import read_task_log

            content = read_task_log(directory)
        content = content or "等待运行日志。旧版本执行的任务可能没有保存日志。"
        if content != logs.toPlainText():
            scrollbar = logs.verticalScrollBar()
            following = scrollbar.value() >= scrollbar.maximum()
            position = scrollbar.value()
            logs.setPlainText(content)
            scrollbar.setValue(scrollbar.maximum() if following else position)

    refresh()
    timer = QTimer(dialog)
    timer.timeout.connect(refresh)
    timer.start(1000)
    dialog.exec()
    timer.stop()
