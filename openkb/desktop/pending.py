"""Pending ordinary work and execution limits, separate from source relationships."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from openkb.application.pending import (
    cancel_execution_group,
    pending_status,
    retry_pending_job,
    update_execution_budget,
)
from openkb.desktop.panels import ManagementPanel


class PendingDialog(ManagementPanel):
    def __init__(self, window, kb):
        super().__init__(window)
        self.window, self.kb = window, kb
        self._closed, self._busy = False, False
        self.groups = {}
        self.checkpoints = []
        self.setWindowTitle("待处理工作")
        self.resize(1050, 720)
        layout = QVBoxLayout(self)
        self.status = QLabel("程序打开时自动处理可运行待办；未知结果需要明确重试。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["工作", "状态", "说明", "标识"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.select)
        layout.addWidget(self.table, 1)
        self.diagnostics = QTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumHeight(130)
        self.diagnostics.setAccessibleName("文件恢复检查点与诊断")
        layout.addWidget(self.diagnostics)
        form = QFormLayout()
        self.fields = {}
        for key, label in (
            ("max_depth", "最大层数"),
            ("max_sources", "来源数上限"),
            ("max_object_bytes", "单文件字节上限"),
            ("max_total_bytes", "累计文件字节上限"),
            ("max_decompressed_bytes", "累计解压字节上限"),
            ("max_discovery_seconds", "发现耗时上限（秒）"),
        ):
            entry = QLineEdit()
            form.addRow(label, entry)
            self.fields[key] = entry
        layout.addLayout(form)
        self.budget_status = QLabel()
        self.budget_status.setWordWrap(True)
        layout.addWidget(self.budget_status)
        buttons = QHBoxLayout()
        for label, callback in (
            ("刷新", self.reload),
            ("明确重试所选工作", self.retry),
            ("更新本组预算", self.budget),
            ("取消整个执行组", self.cancel),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.reload()

    def operate(self, function, callback=None, *, exclusive=False, control=False):
        if self._busy:
            return
        self._busy = True

        def finished(value, error):
            self._busy = False
            if error:
                self.status.setText(str(error))
            elif callback:
                callback(value)
            else:
                self.reload()

        self.window.io.submit(
            function,
            finished,
            kb=None if control else self.kb,
            exclusive=exclusive,
            obsolete=lambda: self._closed,
        )

    def reload(self):
        self.operate(lambda: pending_status(self.kb), self.render)

    def render(self, inventory):
        self.checkpoints = inventory.get("checkpoints", [])
        self.groups = {group["root_import_id"]: group for group in inventory["groups"]}
        inspected = {item["discovery_intent_id"] for item in self.checkpoints}
        rows = [
            job
            for job in inventory["jobs"]
            if job["kind"] == "import" or job["status"] != "completed" or job["id"] in inspected
        ]
        self.table.setRowCount(len(rows))
        for row, job in enumerate(rows):
            for column, value in enumerate(
                (
                    "发现文件" if job["kind"] == "discovery" else job["filename"],
                    {
                        "pending": "等待处理",
                        "dispatched": "等待开始",
                        "started": "处理中",
                        "budget_wait": "等待提高预算",
                        "interrupted": "结果待核实",
                        "blocked": "等待业务条件",
                        "failed": "失败",
                        "partial": "部分完成",
                        "stopped": "已停止",
                        "cancelled": "执行组已取消",
                        "stale": "已失效",
                        "completed": "已完成，有编译缺项"
                        if job.get("quality") or job.get("unfinished")
                        else "已完成",
                    }.get(job["status"], job["status"]),
                    {
                        "Source count budget exhausted": "来源数已达到本组上限",
                        "Object byte budget exhausted": "文件字节数已达到本组上限",
                        "Decompression budget exhausted": "解压字节数已达到本组上限",
                        "Discovery depth budget exhausted": "已达到文件发现层数上限",
                        "Discovery time budget exhausted": "已达到文件发现耗时上限",
                    }.get(job["message"], job["message"] or ""),
                    job["id"],
                )
            ):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, job)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        if rows:
            self.table.selectRow(0)
        self.status.setText(
            f"发现待办 {inventory['discovery_pending']}；"
            f"文件导入待办 {inventory['imports_pending']}；"
            f"可运行 {inventory['runnable']}。"
        )

    def selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select(self):
        job = self.selected()
        if job:
            labels = {
                "recovered": "已恢复完整文件",
                "private_object": "私有对象",
                "preview": "预览图",
                "external_reference": "外链（未下载）",
                "corrupt_object": "损坏对象",
                "requires_container_rebuild": "需要重建标准容器",
                "cycle": "循环已停止",
                "unknown": "旧检查点",
            }
            self.diagnostics.setPlainText(
                "\n".join(
                    f"{labels[item['outcome']]} · {item['object_key']}\n"
                    f"{item.get('diagnostic') or ''}"
                    for item in self.checkpoints
                    if item["discovery_intent_id"] == job["id"]
                )
            )
            if job["kind"] == "import":
                from openkb.llm_usage import describe_model_usage

                lines = [
                    f"任务：{job['id']}",
                    f"文件：{job['filename']}",
                    f"来源：{job.get('source_id') or '尚未接入'}",
                    f"来源修订：{job.get('source_revision_id') or '无'}",
                    "本次编译质量：" + ("已记录" if job.get("quality_known") else "未知"),
                ]
                lines.extend("质量告警：" + note for note in job.get("quality", []))
                lines.extend("未完成：" + stage for stage in job.get("unfinished", []))
                for unit in (job.get("result") or {}).get("units", []):
                    lines.append(
                        f"单元：{unit.get('name') or unit.get('key')}；{unit['unit_id']}；"
                        f"目标修订：{unit['target_revision_id']}"
                    )
                    lines.extend(describe_model_usage(unit.get("model_usage")))
                lines.extend(describe_model_usage(job.get("model_usage")))
                self.diagnostics.setPlainText("\n".join(lines))
            group = self.groups[job["root_import_id"]]
            usage = group["model_usage"]
            self.diagnostics.appendPlainText(
                f"\n主文与附件累计（请求集合）：已知输入 {usage['input_total']}，"
                f"已知输出 {usage['output_total']} tokens；请求 {usage['requests']}，"
                f"用量未知 {usage['unknown_requests']}；账本：{group['usage_ledger']}"
            )
            for key, entry in self.fields.items():
                entry.setText(str(group["budget"][key]))
            self.budget_status.setText(
                f"本组已接入 {group['sources']} 份来源；已恢复 {group['object_bytes']} 字节；"
                f"发现耗时 {group['discovery_seconds']:.2f} 秒。"
                f"预算来源：{group['budget_origin']}。"
            )

    def retry(self):
        job = self.selected()
        if job:
            if (
                job["status"] == "interrupted"
                and QMessageBox.question(
                    self,
                    "重试结果未知的工作",
                    "已提交的结果会保留。未知部分可能重复调用模型并产生费用，继续？",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            self.operate(lambda: retry_pending_job(self.kb, job["id"]), exclusive=True)

    def budget(self):
        job = self.selected()
        if job:
            try:
                patch = {
                    key: float(entry.text())
                    if key == "max_discovery_seconds"
                    else int(entry.text())
                    for key, entry in self.fields.items()
                }
            except ValueError:
                self.status.setText("请输入有效的数字预算。")
                return
            self.operate(
                lambda: update_execution_budget(self.kb, job["root_import_id"], patch),
                exclusive=True,
            )

    def cancel(self):
        job = self.selected()
        if (
            job
            and QMessageBox.question(
                self, "取消执行组", "取消本组所有未完成工作？已经发布的资料仍保留。"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.operate(
                lambda: cancel_execution_group(self.kb, job["root_import_id"]), control=True
            )

    def done(self, result):
        self._closed = True
        super().done(result)
