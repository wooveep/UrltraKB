"""Public answer stages and elapsed time, without model text or private diagnostics."""

from datetime import datetime, timezone
from time import monotonic

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

STAGES = {
    "answering": "正在查阅知识库并整理回答",
    "saving": "正在保存回答",
}


def task_status(task):
    if task.state in {"queued", "waiting", "stopping"}:
        return {
            "queued": "已排队，等待执行",
            "waiting": "等待知识库可用",
            "stopping": "正在安全停止",
        }[task.state]
    return STAGES.get(task.stage, "正在处理问题")


def failure_status(error, *, stopped=False):
    if stopped:
        return "回答已停止。问题已保留，可以继续提问。"
    messages = {"TimeoutError": "等待模型响应超时", "AuthenticationError": "模型认证失败"}
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
        self.steps = QLabel("基于 Wiki 知识与长文索引回答 · 完成后保存到对话历史")
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
