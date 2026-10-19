"""Live task facts, without replaying requests or exposing temporary model output."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
)

from ultrakb.desktop.task_status import (
    STATES,
    pending_item_status,
    processing_progress_title,
    task_progress,
)


def task_text(task, *, include_budget=True):
    lines = [
        task.kb_dir,
        f"任务：{task.id}",
        f"状态：{STATES.get(task.state, task.state)}",
        f"阶段：{task_progress(task)}",
        f"{processing_progress_title(task)}：已处理 {len(task.results)} / {task.total} 项"
        "（按已确认结果计数）",
        f"成功：{task.succeeded} · 跳过：{task.skipped} · "
        f"失败：{task.failed} · 未完成：{task.unfinished}",
        f"错误：{task.error or '无'}",
        f"已请求安全停止：{task.stop_requested}",
        f"进程已回收：{task.processes_reaped}",
    ]
    if task.retry_of:
        lines.append(f"重试来源：{task.retry_of}（独立的新任务）")
    if task.current_source:
        lines.append(f"当前资料：{task.current_source}")
    if work := task.compile_work:
        stage = {"summary": "全文摘要", "planning": "规划", "links": "链接", "repair": "纠错"}.get(
            work["stage"], str(work["stage"])
        )
        lines.append(
            f"{stage}工作：已完成 {work['completed']} · 待处理 {work['pending']} · "
            f"处理中 {work['in_flight']} · 分裂 {work['splits']} · 纠错 {work['repairs']}"
        )
    from ultrakb.ingest_result import describe_units
    from ultrakb.llm_usage import describe_model_usage, is_model_budget_description

    lines.extend(describe_model_usage(task.model_usage, include_budget=include_budget))
    for item in task.not_applicable:
        lines.append(f"不适用（未提交）：{item.name} · {item.reason}")
    for number, result in enumerate(task.results, 1):
        label = {**STATES, "skipped": "已跳过"}.get(result.status, result.status)
        lines.extend(["", f"第 {number} 项：{label}"])
        if result.error:
            lines.append(result.error)
        lines.extend(result.changes)
        lines.extend(describe_units(result.import_units, include_model_budget=include_budget))
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
        lines.extend(describe_model_usage(result.model_usage, include_budget=include_budget))
        lines.extend(f"保留 / 已提交：{path}" for path in result.resources)
    for index in range(len(task.results), task.total):
        lines.append(f"第 {index + 1} 项：{pending_item_status(task, index)}")
    if not include_budget:
        # Old workers also saved plain budget snapshots inside result.changes.
        lines = [line for line in lines if not is_model_budget_description(line)]
    return "\n".join(lines)


class TaskDetailsDialog(QDialog):
    def __init__(self, task, parent):
        super().__init__(parent)
        from ultrakb.desktop.dialog_geometry import resize_to_screen

        self.task, self.manager = task, getattr(parent, "manager", None)
        self.setWindowTitle("任务结果")
        resize_to_screen(self, 820, 620)
        layout = QVBoxLayout(self)
        self.progress_group = QGroupBox("资料处理进度")
        progress_layout = QVBoxLayout(self.progress_group)
        self.progress_status = QLabel()
        self.progress_status.setWordWrap(True)
        self.progress_status.setTextFormat(Qt.TextFormat.PlainText)
        progress_layout.addWidget(self.progress_status)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("已确认资料处理结果")
        self.progress.setFormat("%v / %m 项已处理（含失败和跳过）")
        progress_layout.addWidget(self.progress)
        layout.addWidget(self.progress_group)
        self.budget_group = QGroupBox("模型调用预算")
        budget_layout = QVBoxLayout(self.budget_group)
        self.budget = QLabel()
        self.budget.setWordWrap(True)
        self.budget.setTextFormat(Qt.TextFormat.PlainText)
        self.budget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        budget_layout.addWidget(self.budget)
        layout.addWidget(self.budget_group)
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
        from ultrakb.desktop.task_status import task_counts
        from ultrakb.llm_usage import describe_model_budget

        task = self.task
        self.progress_group.setTitle(processing_progress_title(task))
        self.progress.setRange(0, task.total)
        self.progress.setValue(len(task.results))
        self.progress_status.setText(
            task_counts(task)
            + f"\n{STATES[task.state]} · {task_progress(task)}"
            + (f"\n当前资料：{task.current_source}" if task.current_source else "")
        )
        budget = describe_model_budget(task.model_usage)
        self.budget.setText(
            "\n".join(budget) if budget else "尚无调用预算记录。调用次数上限不代表资料完成进度。"
        )
        content = task_text(task, include_budget=False)
        if content == self.text.toPlainText():
            return
        bar = self.text.verticalScrollBar()
        position, follow = bar.value(), bar.value() >= bar.maximum() - 2
        cursor = self.text.textCursor()
        self.text.setPlainText(content)
        self.text.setTextCursor(cursor)
        bar.setValue(bar.maximum() if follow else position)


def show_task_details(task, parent):
    from pathlib import Path

    if (Path(task.kb_dir) / ".ultrakb/kb.sqlite").is_file():
        from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

        executions = (task.model_usage or {}).get("execution_ids", [])
        KnowledgeTasksDialog(
            task.kb_dir,
            parent,
            execution_id=executions[-1] if executions else None,
            select_first=bool(executions),
        ).exec()
        return
    TaskDetailsDialog(task, parent).exec()
