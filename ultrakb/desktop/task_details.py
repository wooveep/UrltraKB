"""Live task facts, without replaying requests or exposing temporary model output."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout

from ultrakb.desktop.task_status import STATES, task_progress


def task_text(task):
    lines = [
        task.kb_dir,
        f"任务：{task.id}",
        f"状态：{STATES.get(task.state, task.state)}",
        f"阶段：{task_progress(task)}",
        f"成功：{task.succeeded} · 跳过：{task.skipped} · "
        f"失败：{task.failed} · 未处理：{task.unfinished}",
        f"错误：{task.error or '无'}",
        f"已请求安全停止：{task.stop_requested}",
        f"进程已回收：{task.processes_reaped}",
    ]
    if task.retry_of:
        lines.append(f"重试来源：{task.retry_of}（独立的新任务）")
    if task.current_source:
        lines.append(f"当前资料：{task.current_source}")
    from ultrakb.ingest_result import describe_units
    from ultrakb.llm_usage import describe_model_usage

    lines.extend(describe_model_usage(task.model_usage))
    for item in task.not_applicable:
        lines.append(f"不适用（未提交）：{item.name} · {item.reason}")
    for number, result in enumerate(task.results, 1):
        lines.extend(["", f"第 {number} 项：{result.status}"])
        if result.error:
            lines.append(result.error)
        lines.extend(result.changes)
        lines.extend(describe_units(result.import_units))
        for item in result.removal_items:
            status = {
                "completed": "已移除",
                "failed": "未完成",
                "not_run": "未执行",
                "partial": "部分完成",
            }[item.status]
            lines.append(f"{item.name} · {status} · {item.message or ''}")
            lines.extend(f"保留：{path}" for path in item.retained)
            lines.extend(item.changes)
            lines.extend(f"未完成：{stage}" for stage in item.unfinished)
        if result.removal_items:
            completed = sum(item.status == "completed" for item in result.removal_items)
            failed = sum(item.status in {"failed", "partial"} for item in result.removal_items)
            pending = sum(item.status == "not_run" for item in result.removal_items)
            lines.append(f"移除逐项结果：已完成 {completed} · 未完成 {failed} · 未执行 {pending}")
        lines.extend(f"未完成：{stage}" for stage in result.unfinished)
        lines.extend(f"质量检查：{note}" for note in result.quality)
        if result.quality_known is False:
            lines.append("本次编译质量：未知（没有保存回执）")
        lines.extend(describe_model_usage(result.model_usage))
        lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
    for index in range(len(task.results), task.total):
        if task.state == "interrupted" and index == len(task.results):
            detail = "结果未确认；请核对保留成果，不能当作未执行。"
        elif task.state in {"completed", "partial", "stopped", "blocked", "failed", "interrupted"}:
            detail = "未执行；本批已停止，不会自动续跑。"
        else:
            detail = "等待执行。"
        lines.append(f"第 {index + 1} 项：{detail}")
    return "\n".join(lines)


class TaskDetailsDialog(QDialog):
    def __init__(self, task, parent):
        super().__init__(parent)
        from ultrakb.desktop.dialog_geometry import resize_to_screen

        self.task, self.manager = task, getattr(parent, "manager", None)
        self.setWindowTitle("任务结果")
        resize_to_screen(self, 820, 620)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.finished.connect(self.timer.stop)
        self.refresh()
        self.timer.start()

    def refresh(self):
        if self.manager is not None:
            try:
                self.task = self.manager.get(self.task.id)
            except KeyError:
                self.timer.stop()
                self.setWindowTitle("任务摘要已清理")
                return
        content = task_text(self.task)
        if content == self.text.toPlainText():
            return
        bar = self.text.verticalScrollBar()
        position, follow = bar.value(), bar.value() >= bar.maximum() - 2
        cursor = self.text.textCursor()
        self.text.setPlainText(content)
        self.text.setTextCursor(cursor)
        bar.setValue(bar.maximum() if follow else position)


def show_task_details(task, parent):
    TaskDetailsDialog(task, parent).exec()
