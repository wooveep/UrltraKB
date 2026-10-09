"""Readable task progress; provider and exception diagnostics stay in details."""


def task_progress(task, stage_label: str) -> str:
    units = [unit for result in task.results for unit in result.import_units]
    incomplete = [unit for unit in units if unit.status not in {"completed", "empty", "retired"}]
    parts = []
    if any("indexing" in unit.completed_stages for unit in incomplete):
        parts.append("索引已完成，知识编译未完成；已保留重试进度")
    reasons = {
        "ModelBudgetExceeded": "模型调用已达到任务上限",
        "ModelDeadlineExceeded": "任务时限已到",
        "LockCancelled": "已安全停止",
        "CancelledError": "已安全停止",
        "CompilationIncomplete": "原文读取或知识编译尚未完成",
    }
    for unit in incomplete:
        reason = reasons.get(unit.error_type)
        if reason and reason not in parts:
            parts.append(reason)
    if parts:
        return "；".join(parts)
    return "执行未完成，可查看详情并重试" if task.error else stage_label
