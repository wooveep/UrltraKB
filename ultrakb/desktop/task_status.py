"""Shared, readable task status for the workspace, import progress and live details."""

from pathlib import Path

STATES = {
    "queued": "排队中",
    "waiting": "等待知识库",
    "running": "执行中",
    "stopping": "正在安全停止",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "意外中断",
    "blocked": "需要修复",
}
OPERATIONS = {
    "SavePage": "保存页面",
    "AskQuestion": "问答",
    "ContinueConversation": "对话",
    "ImportFile": "导入资料",
    "ImportUrl": "导入网址",
    "RemoveDocument": "移除资料",
    "RecompileDocument": "重新生成知识",
    "RetrySource": "重试资料",
    "RetryWorksheet": "重试工作表",
    "ReprocessDocument": "重新处理资料",
    "DeleteConversation": "删除对话",
    "ExportConversation": "导出对话",
    "CheckKnowledge": "知识检查与修复",
    "RunPendingJob": "处理待完成内容",
}
STAGES = {
    **STATES,
    "preparing": "准备资料",
    "converting": "转换与识别资料",
    "indexing": "建立长文索引",
    "compiling": "编译摘要、概念与实体",
    "committed": "已保存到知识库",
    "answering": "查阅知识库并整理回答",
    "retrieving": "检索相关内容",
    "reading": "阅读依据",
    "saving": "保存结果",
    "waiting-input": "等待文件稳定",
    "results-confirmed": "已确认结果",
    "input-returned-to-watch": "输入仍在变化，已交回监听补查",
}


def task_progress(task, stage_label: str | None = None) -> str:
    from ultrakb.runtime.records import TERMINAL

    if task.pending_stops:
        return "停止信号等待确认；自动派发已暂停，可再次确认停止。"
    label = stage_label or STAGES.get(task.stage, "正在处理")
    if task.state not in TERMINAL:
        return label
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
    return "执行未完成，可查看详情并重试" if task.error else label


def task_counts(task):
    return (
        f"已处理 {len(task.results)} / {task.total} · 成功 {task.succeeded} · "
        f"跳过 {task.skipped} · 失败 {task.failed} · 未完成 {task.unfinished}"
    )


def processing_progress_title(task):
    documents = {
        "ImportFile",
        "ImportUrl",
        "RecompileDocument",
        "ReprocessDocument",
        "RetrySource",
        "RetryWorksheet",
    }
    return "资料处理进度" if task.operation in documents else "任务处理进度"


def pending_item_status(task, index):
    """Only the first unconfirmed unit can be active in a sequential batch."""
    from ultrakb.runtime.records import TERMINAL

    current = index == len(task.results)
    if task.pending_stops:
        return "停止信号等待确认；自动派发已暂停。"
    if task.state == "interrupted" and current:
        return "结果未确认；请核对保留成果，不能当作未执行。"
    if task.state in TERMINAL:
        return "未执行；本批已停止，不会自动续跑。"
    stopping = task.state == "stopping" or task.stop_requested
    if current:
        if stopping:
            return "正在安全停止，等待资源清理与结果确认。"
        if task.state == "waiting":
            return "等待知识库可用，随后继续处理。"
        if task.state == "running":
            return f"正在执行 · {task_progress(task)}；结果尚未确认。"
    if stopping:
        return "尚未执行；已请求停止，不会自动开始。"
    return "等待执行。"


def current_source(task):
    return Path(task.current_source).name if task.current_source else ""


def needs_attention(task):
    from ultrakb.runtime.records import TERMINAL

    return (
        bool(task.pending_stops)
        or task.state in TERMINAL
        and (task.state != "completed" or any(r.quality or r.unfinished for r in task.results))
    )
