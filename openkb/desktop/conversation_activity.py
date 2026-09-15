"""Public answer stages and elapsed time, without model text or private diagnostics."""

from datetime import datetime, timezone
from time import monotonic

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

STAGES = {
    "answering": "正在准备回答",
    "answer_sources": "正在查找与阅读资料",
    "answer_drafting": "正在整理回答",
    "answer_review": "正在核对原文与引用",
    "answer_repair": "正在修正并复核回答",
    "answer_saving": "正在保存回答",
}


def task_status(task):
    if task.state in {"queued", "waiting", "stopping"}:
        return {
            "queued": "已排队，等待执行",
            "waiting": "等待知识库可用",
            "stopping": "正在安全停止",
        }[task.state]
    label = STAGES.get(task.stage, "正在处理问题")
    for step in task.progress:
        if step.phase == "answer_review" and step.total is not None:
            label += f" · 已核对 {step.completed}/{step.total} 段"
    return label


def failure_status(error, *, stopped=False):
    if stopped:
        return "回答已停止。问题已保留，可以继续提问。"
    messages = {
        "answer_verification_invalid:quote_mismatch": "回答校验未完成：核对引用与原文不一致",
        "answer_verification_invalid:support_mismatch": "回答校验未完成：核对证据关联无效",
        "answer_verification_invalid:verdict_issues_conflict": "回答校验未完成：核对结果自相矛盾",
        "InvalidReview": "回答校验未完成：校验结果的格式或证据关联无效",
        "answer_verification_invalid": "回答校验未完成：校验结果的格式或证据关联无效",
        "answer_evidence_unsupported": "回答仍有未通过原文核对的内容",
        "answer_citation_invalid": "回答中有无法核对的引用",
        "request_timeout": "等待模型响应超时",
        "time_budget_exhausted": "本轮回答达到处理时限",
        "request_budget_exhausted": "本轮回答达到请求次数上限",
        "token_budget_exhausted": "本轮回答达到用量上限",
        "output_budget_exhausted": "模型返回的回答被截断",
        "answer_empty": "模型没有返回可用的回答",
    }
    label = next(
        (text for code, text in messages.items() if code in (error or "")), "本次回答未完成"
    )
    return label + "。问题已保留，可重试或在任务中查看原因。"


class ConversationActivity(QWidget):
    """Update a small status surface independently from Markdown rendering."""

    def __init__(self):
        super().__init__()
        self._task = None
        self._started = monotonic()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.label)
        self.steps = QLabel("查找资料  ›  整理回答  ›  核对原文与引用  ›  保存")
        self.steps.setWordWrap(True)
        self.steps.setObjectName("muted")
        layout.addWidget(self.steps)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        layout.addWidget(self.bar)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.setText("")

    def set_task(self, task):
        if self._task is None or self._task.id != task.id:
            self._started = monotonic()
        self._task = task
        self.steps.show()
        self.bar.show()
        if not self.timer.isActive():
            self.timer.start()
        self.tick()

    def tick(self):
        if self._task is None:
            return
        seconds = monotonic() - self._started
        if self._task.started_at:
            try:
                started = datetime.fromisoformat(self._task.started_at)
                seconds = (datetime.now(timezone.utc) - started).total_seconds()
            except (ValueError, TypeError):
                pass
        seconds = max(0, int(seconds))
        elapsed = f"{seconds // 60} 分 {seconds % 60:02d} 秒" if seconds >= 60 else f"{seconds} 秒"
        self.label.setText(task_status(self._task) + " · 已用时 " + elapsed)
        self.setAccessibleName(self.label.text())

    def setText(self, text):
        self._task = None
        self.timer.stop()
        self.label.setText(text)
        self.steps.hide()
        self.bar.hide()

    def text(self):
        return self.label.text()
