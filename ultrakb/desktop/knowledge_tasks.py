"""Bounded, off-thread main-store task history with shared JSON export."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ultrakb.application.knowledge_tasks import (
    capture_task_identity,
    export_knowledge_task,
    list_knowledge_tasks,
    read_knowledge_execution,
    read_knowledge_task,
)
from ultrakb.desktop.io import LocalIO

_STATES = {
    "started": "执行中",
    "stopping": "正在安全停止",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
    "reused": "已复用",
}
_STAGES = {
    "import.admit.v1": "接纳原件",
    "import.process.v1": "转换与解析",
    "import.reprocess.v1": "重新处理保留原件",
    "workbook.inventory.v1": "读取工作表清单",
    "worksheet.normalize.v1": "整理工作表原文",
    "import.retain_original.v1": "保留原件和原图",
    "compile.inputs.v1": "准备编译输入",
    "compile.generate.v1": "生成知识",
    "compile.validate.v1": "校验知识",
    "compile.persist.v1": "保存候选成果",
    "knowledge.publish.v1": "发布知识",
    "wiki.sync.v1": "同步工作副本",
    "execution.cleanup.v1": "清理与收尾",
    "model_request.v1": "模型请求",
}
_PAGES = {"executions": "执行", "stages": "阶段", "requests": "请求", "publications": "发布"}


def _usage(label, usage):
    if usage is None:
        return [f"{label}：尚无记录"]
    return [
        f"{label} · 已知发送：{usage.actual_sends} · 发送未知：{usage.send_unknown}",
        f"已知输入：{usage.input_total} token · 已知输出：{usage.output_total} token",
        f"用量未知请求：{usage.unknown_requests} · 尚未结束：{usage.in_flight_requests}",
    ]


def detail_text(detail):
    task, budget, execution = detail.task, detail.task.budget, detail.execution
    lines = [
        f"本次执行状态：{_STATES.get(execution.state, execution.state)}",
        f"任务最新状态：{_STATES.get(task.state, task.state)}",
        f"开始：{execution.started_at} · 结束：{execution.ended_at or '未记录'}",
        f"预算已用：{budget.used} · "
        f"剩余：{budget.remaining if budget.remaining is not None else '未设上限'}",
        *_usage("本次执行", detail.execution_usage),
        *_usage("任务累计", task.usage),
        "",
        "当前阶段页（父子阶段耗时不可相加作为任务耗时）：",
    ]
    if execution.application_task_id is not None:
        lines[0:0] = [
            f"应用任务：{execution.application_task_id}",
            f"工作单元：{execution.application_unit_id or '未记录'}",
            *(
                [f"重试来源任务：{execution.parent_application_task_id}"]
                if execution.parent_application_task_id is not None
                else []
            ),
        ]
    for stage in detail.stages:
        elapsed = (
            f"{stage.duration_seconds:.3f} 秒" if stage.duration_seconds is not None else "未知"
        )
        lines.append(
            f"{_STAGES.get(stage.stage_code, stage.stage_code)} · "
            f"{_STATES.get(stage.state, stage.state)} · 耗时 {elapsed}"
        )
    lines.extend(
        ["", f"当前请求页：{len(detail.requests)} 条；发布页：{len(detail.publications)} 条。"]
    )
    for publication in detail.publications:
        status = {
            "pending": "待同步",
            "paused": "外部编辑已暂停同步",
            "conflict": "同步冲突",
            "synced": "已同步",
            "superseded": "已被后续发布取代",
            "not_recorded": "未记录同步状态",
        }[publication.projection_status]
        if publication.projection_details == "retired":
            status += " · 同步明细已回收"
        lines.append(f"{publication.published_at} · {status}")
    lines.extend(
        [
            "",
            "此页有后续记录。" if not detail.coverage["page_complete"] else "此页没有后续记录。",
            "非模型阶段覆盖：部分；资源采样和价格：未记录。未知用量不代表免费。",
            f"统计时点：{detail.statistics_at} · 导出版本：{detail.schema_version}",
        ]
    )
    return "\n".join(lines)


class KnowledgeTasksDialog(QDialog):
    def __init__(
        self, kb_dir, parent=None, *, task_id=None, execution_id=None, limit=63, select_first=True
    ):
        super().__init__(parent)
        self.root, self.limit = Path(kb_dir), limit
        self.task_id, self.execution_id = task_id, execution_id
        self.select_first = select_first
        self.identity, self.detail, self.list_cursor = None, None, None
        self.cursors = {name + "_after": 0 for name in _PAGES}
        self._closed, self._busy, self._sequence = False, False, 0
        self.io = LocalIO(self)
        self.setWindowTitle("任务记录")
        self.resize(860, 720)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取任务记录…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tasks = QListWidget()
        self.tasks.setMaximumHeight(130)
        self.tasks.setAccessibleName("知识库任务记录")
        self.tasks.currentItemChanged.connect(self.select_task)
        layout.addWidget(self.tasks)
        self.executions = QComboBox()
        self.executions.setAccessibleName("选择执行")
        self.executions.currentIndexChanged.connect(self.select_execution)
        layout.addWidget(self.executions)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        if not select_first:
            self.body.setPlainText("尚未关联到此任务的执行。可从知识库任务列表显式选择记录。")
        layout.addWidget(self.body, 1)
        navigation = QHBoxLayout()
        self.next_buttons = {}
        for name, label in _PAGES.items():
            button = QPushButton(f"下一页{label}")
            button.clicked.connect(lambda checked=False, section=name: self.next_page(section))
            navigation.addWidget(button)
            self.next_buttons[name] = button
        layout.addLayout(navigation)
        controls = QHBoxLayout()
        for label, action in (
            ("任务列表", lambda: self.load_list()),
            ("下一页任务", lambda: self.load_list(self.list_cursor or 0)),
            ("刷新本页", self.refresh),
            ("从首页查看", self.reset_pages),
            ("导出本页 JSON", self.choose_export),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            controls.addWidget(button)
        self.subscription = QPushButton("暂停刷新")
        self.subscription.setCheckable(True)
        self.subscription.toggled.connect(
            lambda paused: self.subscription.setText("恢复刷新" if paused else "暂停刷新")
        )
        controls.addWidget(self.subscription)
        layout.addLayout(controls)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.finished.connect(self.stop)
        self.refresh()

    def submit(self, operation, loaded):
        self._sequence += 1
        sequence = self._sequence
        self._busy = True

        def completed(value, error):
            self._busy = False
            if error:
                self.status.setText(str(error))
            else:
                loaded(value)

        # The shared query owns its v2 lifecycle/reclaim stage. A legacy outer
        # kb_lock would reject the format and would also obstruct bounded reads.
        self.io.submit(
            operation,
            completed,
            cancelled=lambda: self._closed,
            obsolete=lambda: self._closed or sequence != self._sequence,
        )

    def options(self):
        return dict(
            identity=self.identity,
            limit=self.limit,
            execution_id=self.execution_id,
            cancelled=lambda: self._closed,
            **self.cursors,
        )

    def refresh(self):
        options = self.options()
        task_id, execution_id = self.task_id, self.execution_id
        if task_id is None and execution_id is None:
            self.load_list()
            return

        def read():
            identity = options["identity"] or capture_task_identity(self.root)
            options["identity"] = identity
            if task_id is None:
                options.pop("execution_id")
                detail = read_knowledge_execution(self.root, execution_id, **options)
            else:
                detail = read_knowledge_task(self.root, task_id, **options)
            return identity, detail

        def loaded(value):
            self.identity, self.detail = value
            self._shown_options = {
                **options,
                "identity": self.identity,
                "execution_id": self.detail.execution_id,
            }
            self.task_id = self.detail.task.task_id
            self.body.setPlainText(detail_text(self.detail))
            self.status.setText("任务记录 · 分页读取权威数据；暂停刷新不会停止记录。")
            self.executions.blockSignals(True)
            self.executions.clear()
            self.executions.addItem("最新执行", None)
            choices = {execution.execution_id: execution for execution in self.detail.executions}
            if self.execution_id is not None:
                choices.setdefault(self.detail.execution_id, self.detail.execution)
            for execution in choices.values():
                self.executions.addItem(
                    f"{execution.started_at} · {_STATES.get(execution.state, execution.state)}",
                    execution.execution_id,
                )
            self.executions.setCurrentIndex(max(0, self.executions.findData(self.execution_id)))
            self.executions.blockSignals(False)
            for name, button in self.next_buttons.items():
                button.setEnabled(getattr(self.detail, name + "_after") is not None)

        self.submit(read, loaded)

    def load_list(self, after=0):
        identity = self.identity

        def read():
            bound = identity or capture_task_identity(self.root)
            return bound, list_knowledge_tasks(
                self.root,
                identity=bound,
                after=after,
                limit=self.limit,
                cancelled=lambda: self._closed,
            )

        def loaded(value):
            self.identity, page = value
            self.list_cursor = page.next_cursor
            self.tasks.blockSignals(True)
            self.tasks.clear()
            for task in page.tasks:
                item = QListWidgetItem(f"{task.created_at} · {_STATES[task.state]}")
                item.setData(Qt.ItemDataRole.UserRole, task.task_id)
                self.tasks.addItem(item)
            self.tasks.blockSignals(False)
            self.status.setText("任务列表 · " + ("还有下一页" if page.next_cursor else "已到末页"))
            if page.tasks and self.select_first:
                self.tasks.setCurrentRow(0)

        self.submit(read, loaded)

    def select_task(self, item, previous=None):
        if item is not None:
            self.task_id = item.data(Qt.ItemDataRole.UserRole)
            self.execution_id = None
            self.reset_pages()

    def select_execution(self):
        self.execution_id = self.executions.currentData()
        self.reset_pages()

    def next_page(self, section):
        if self.detail and (cursor := getattr(self.detail, section + "_after")) is not None:
            self.cursors[section + "_after"] = cursor
            self.refresh()

    def reset_pages(self):
        self.cursors = {name + "_after": 0 for name in _PAGES}
        self.refresh()

    def poll(self):
        if not self._busy and not self.subscription.isChecked() and self.task_id:
            self.refresh()

    def choose_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出任务页", "task.json", "JSON (*.json)")
        if path:
            self.export_to(Path(path))

    def export_to(self, destination):
        if self.detail is None:
            return
        options, task_id = dict(self._shown_options), self.detail.task.task_id
        self.submit(
            lambda: export_knowledge_task(self.root, task_id, Path(destination), **options),
            lambda result: self.status.setText("已导出当前分页范围的任务记录。"),
        )

    def stop(self, result=None):
        self._closed = True
        self.timer.stop()
        self.io.stop()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
