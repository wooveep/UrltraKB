"""Review the shared processing preview, then submit its exact version to the worker."""

import json

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from openkb.application.reprocessing import preview_reprocessing
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ReprocessSource


def describe_preview(preview):
    lines = [
        preview["name"],
        "原件：" + ("已保存且校验通过" if preview["original"]["available"] else "不可用"),
    ]
    lines.extend(
        f"资产 {item['reference']}：{'可用' if item['available'] else item['reason']}"
        for item in preview["assets"]
    )
    if preview["runtime"]["required"]:
        lines.append(
            "Office 运行时："
            + (
                "文件校验通过；执行时将检查启动能力"
                if preview["runtime"]["available"]
                else preview["runtime"]["reason"]
            )
        )
    lines.append(
        "处理范围："
        + (
            "重新读取全部工作表；失败单元保留旧成功结果"
            if preview["range"] == "all_worksheets"
            else "整份资料正文"
        )
    )
    lines.append("重新处理会保留来源记录；人工修改仍通过待接受差异确认。")
    if preview["version_impact"].get("superseded_proposals"):
        lines.append("所选资料尚有待接受差异；新处理将使这些旧建议过期，人工正文仍保留。")
    for unit in preview["units"]:
        lines.append(
            f"\n{unit['name']}："
            + ("策略已变化" if unit["policy_changed"] else "策略相同，重新处理仍会创建新修订")
        )
        before, after = unit["previous_policy"], unit["next_policy"]
        for key, label in (
            ("classification", "长短分类"),
            ("capacity", "执行容量"),
            ("index_model", "索引模型"),
            ("sheet", "工作表解析策略"),
            ("resources", "远程资产策略"),
        ):
            if before.get(key) != after.get(key):
                lines.append(
                    f"{label}：{json.dumps(before.get(key), ensure_ascii=False)} → "
                    f"{json.dumps(after.get(key), ensure_ascii=False)}"
                )
        if before.get("office") != after.get("office"):
            lines.append("Office 运行时、字体或转换策略已变化。")
    discovery = preview["discovery"]
    if discovery["will_schedule"]:
        lines.extend(
            [
                "\n将创建独立的文件提取组；旧待办保留，提取文件按独立来源导入。",
                "本次提取预算：" + json.dumps(discovery["budget"], ensure_ascii=False),
            ]
        )
    lines.extend("阻止执行：" + reason for reason in preview["blockers"])
    return "\n".join(lines)


class ReprocessingDialog(QDialog):
    def __init__(self, window, kb, source_id, *, scope=None):
        super().__init__(window)
        self.window, self.kb, self.source_id, self.scope = window, kb, source_id, scope
        self._closed, self._busy, self._task, self._preview = False, False, None, None
        self.setWindowTitle("预览重新处理")
        self.resize(740, 540)
        layout = QVBoxLayout(self)
        self.status = QLabel("预览不会转换资料或调用模型。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        actions = QHBoxLayout()
        self.reload_button = QPushButton("刷新预览")
        self.confirm_button = QPushButton("确认重新处理")
        self.confirm_button.setEnabled(False)
        self.reload_button.clicked.connect(self.reload)
        self.confirm_button.clicked.connect(self.execute)
        actions.addWidget(self.reload_button)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def reload(self):
        if self._busy or self._task:
            return
        self._busy, self._preview = True, None
        self.confirm_button.setEnabled(False)
        self.reload_button.setEnabled(False)

        def loaded(value, error):
            self._busy = False
            self.reload_button.setEnabled(True)
            if error:
                self.status.setText(str(error))
                return
            self._preview = value
            self.details.setPlainText(describe_preview(value))
            self.confirm_button.setEnabled(value["status"] == "ready")
            self.status.setText(
                "请核对下列处理范围与策略后确认。"
                if value["status"] == "ready"
                else "当前不能重新处理，请查看原因。"
            )

        self.window.io.submit(
            lambda: preview_reprocessing(self.kb, self.source_id, scope=self.scope),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def execute(self):
        if self._busy or self._task or not self._preview or self._preview["status"] != "ready":
            return
        self._task = self.window.manager.submit(
            self.kb,
            [
                ReprocessSource(
                    self.source_id,
                    self._preview["version"],
                    view_id=self.scope.view_id if self.scope else None,
                )
            ],
        )
        self._preview = None
        self.confirm_button.setEnabled(False)
        self.reload_button.setEnabled(False)
        self.status.setText("已提交重新处理，可在任务面板查看进度或停止。")

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        self.reload_button.setEnabled(True)
        self.status.setText(
            "任务完成。"
            if task.state == "completed"
            else "未全部完成；成功结果已保留，请查看任务详情。"
        )
        for result in task.results:
            self.details.appendPlainText(
                "\n".join([*(result.changes or ()), *(result.unfinished or ()), result.error or ""])
            )

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
