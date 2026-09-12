"""Present measured stage progress, separately from a task's business outcome."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from openkb.runtime.records import TERMINAL

_PHASES = {
    "docx": "DOCX 解析",
    "pdf": "PDF 解析",
    "text": "文本解析",
    "image_ocr": "图片 OCR",
    "cloud_ocr": "等待云端 OCR",
    "facts": "提取知识",
    "planning": "规划知识主题",
    "generation": "生成知识页面",
    "parse_cache": "校验已保存解析",
}
_UNITS = {
    "paragraphs": "段",
    "pages": "页",
    "lines": "行",
    "characters": "字符",
    "topics": "篇",
    "items": "项",
}
_STATES = {
    "queued": "等待开始",
    "waiting": "等待执行",
    "stopping": "正在安全停止",
    "partial": "部分完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "已中断",
    "blocked": "需要处理",
}


def progress_presentation(task):
    """A percentage always names its measured phase; it is never an ETA."""
    details = [f"任务项：已返回结果 {len(task.results)}/{task.total}"]
    if task.state == "completed":
        return 100, "任务完成 · 100%", "\n".join(details)
    for step in task.progress:
        name = _PHASES[step.phase]
        if step.total is None:
            details.append(name + " · 进度未知")
        else:
            details.append(
                f"{name}：{step.percent}%（{step.completed}/{step.total} {_UNITS[step.unit]}）"
            )
    measured = next((step for step in task.progress if step.total is not None), None)
    if measured is not None:
        text = (
            f"{_PHASES[measured.phase]} · {measured.percent}%"
            f"（{measured.completed}/{measured.total} {_UNITS[measured.unit]}）"
        )
        if task.state in TERMINAL or task.state == "stopping":
            text = _STATES[task.state] + " · " + text
        return measured.percent, text, "\n".join(details)
    name = _PHASES[task.progress[-1].phase] if task.progress else "等待进度信息"
    return None, _STATES.get(task.state, name), "\n".join(details)


def update_progress_bar(bar: QProgressBar, task):
    percent, text, detail = progress_presentation(task)
    # Terminal/queued unknown work is static; only an active unknown stage pulses.
    busy = percent is None and task.state in {"running", "waiting", "stopping"}
    bar.setRange(0, 0 if busy else 100)
    bar.setValue(percent if percent is not None else 0)
    bar.setFormat(text)
    bar.setToolTip(detail)
    bar.setAccessibleName(text)
    bar.setAccessibleDescription(detail)


class TaskProgressCell(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.bar = QProgressBar()
        self.bar.setObjectName("task-row-progress")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)

    def update_task(self, task):
        update_progress_bar(self.bar, task)
        _, text, detail = progress_presentation(task)
        if len(task.progress) > 1:
            text += "\n" + _PHASES[task.progress[-1].phase]
        self.label.setText(text)
        self.label.setMinimumHeight(self.label.sizeHint().height())
        self.setToolTip(detail)


def update_task_progress(table, row, task):
    cell = table.cellWidget(row, 5)
    if not isinstance(cell, TaskProgressCell):
        cell = TaskProgressCell(table)
        table.setCellWidget(row, 5, cell)
    cell.update_task(task)
    table.setRowHeight(row, max(64, cell.label.sizeHint().height() + 35))
