"""Task evidence remains separate from temporary model output."""

from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout


def show_task_details(task, parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("任务结果")
    dialog.resize(820, 620)
    layout = QVBoxLayout(dialog)
    text = QPlainTextEdit()
    text.setReadOnly(True)
    lines = [
        task.kb_dir,
        f"任务：{task.id}",
        f"状态：{task.state}",
        f"进程已回收：{task.processes_reaped}",
    ]
    if task.retry_of:
        lines.append(f"重试来源：{task.retry_of}（独立的新任务）")
    for number, result in enumerate(task.results, 1):
        lines.extend(["", f"第 {number} 项：{result.status}"])
        if result.error:
            lines.append(result.error)
        lines.extend(result.changes)
        lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        lines.extend(f"质量检查：{note}" for note in result.quality)
        lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
    text.setPlainText("\n".join(lines))
    layout.addWidget(text)
    dialog.exec()
