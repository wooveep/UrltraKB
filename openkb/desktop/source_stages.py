"""Stage-specific inspection and actions for the retained-source dialog."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openkb.desktop.flow_layout import FlowLayout
from openkb.desktop.source_flow import SourceFlow
from openkb.desktop.source_flow_state import STAGES, STATE_LABELS, flow_steps
from openkb.desktop.source_presentation import REASONS


class SourceStages:
    def build_flow_header(self, layout):
        self._selected_stage = self._initial_stage
        self._activity = None
        self._artifact_offset, self._artifact_next = 0, None
        self._artifact_request = 0
        self.flow = SourceFlow(self)
        self.flow.activated.connect(self.show_stage)
        layout.addWidget(self.flow)
        self.stage_hint = QLabel("点击上方阶段，查看对应的内容与操作。")
        self.stage_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.stage_hint.setWordWrap(True)
        layout.addWidget(self.stage_hint)
        actions = FlowLayout()
        self.stage_buttons = {}
        for key, label, callback in (
            ("export", "导出原文…", self.export),
            ("ocr", "识别设置", self.ocr_settings),
            ("continue", "继续处理（复用已完成内容）", self.continue_saved),
            ("reparse", "重新解析", self.reparse),
            ("navigation", "重建导航", self.rebuild_navigation),
            ("logs", "任务与日志", self.show_source_task),
            ("stop", "安全停止", self.stop_source_task),
        ):
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setObjectName("sourceAction_" + key)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.stage_buttons[key] = button
        self.stage_buttons["continue"].setObjectName("primaryAction")
        self.stage_buttons["continue"].setToolTip("从资料实际未完成的位置继续，不跳过前置阶段。")
        layout.addLayout(actions)

    def build_stage_content(self):
        records = QWidget()
        layout = QVBoxLayout(records)
        self.record_status = QLabel()
        self.record_status.setWordWrap(True)
        layout.addWidget(self.record_status)
        self.records = QComboBox()
        self.records.setAccessibleName("选择已保存的阶段结果")
        self.records.currentIndexChanged.connect(self.display_record)
        layout.addWidget(self.records)
        controls = QHBoxLayout()
        self.record_previous = QPushButton("上一组结果")
        self.record_next = QPushButton("下一组结果")
        self.record_previous.clicked.connect(
            lambda: self.load_artifacts(max(0, self._artifact_offset - 10))
        )
        self.record_next.clicked.connect(lambda: self.load_artifacts(self._artifact_next))
        controls.addWidget(self.record_previous)
        controls.addWidget(self.record_next)
        controls.addStretch()
        layout.addLayout(controls)
        self.record_text = QPlainTextEdit()
        self.record_text.setReadOnly(True)
        layout.addWidget(self.record_text, 1)
        self.record_empty_space = QWidget()
        layout.addWidget(self.record_empty_space, 1)
        self.tabs.addTab(records, "已保存的阶段结果")
        published = QWidget()
        layout = QVBoxLayout(published)
        self.published_status = QLabel("尚无本轮已入库的知识页面。")
        self.published_status.setWordWrap(True)
        layout.addWidget(self.published_status)
        self.published_pages = QComboBox()
        self.published_pages.setAccessibleName("选择已入库知识页")
        layout.addWidget(self.published_pages)
        self.open_published = QPushButton("打开知识页")
        self.open_published.clicked.connect(self.open_published_page)
        layout.addWidget(self.open_published)
        layout.addStretch()
        self.tabs.addTab(published, "已入库知识")

    def source_activity(self):
        if not self._saved:
            return None
        observer = getattr(self.window.manager, "source_activity", None)
        source = self._saved["source"]
        return (
            observer(self.kb, self.source_id, source["id"], source.get("origin"))
            if observer
            else None
        )

    def refresh_flow(self):
        self._activity = self.source_activity()
        steps = flow_steps(self._saved, self._activity)
        if self._selected_stage is None:
            self._selected_stage = next((s.key for s in steps if s.current), "intake")
        self.flow.display(self._saved, self._activity, selected=self._selected_stage)
        selected = next(s for s in steps if s.key == self._selected_stage)
        hint = next(hint for key, _, hint in STAGES if key == selected.key)
        result = (self._saved or {}).get("result") or {}
        self.accept.setEnabled(bool(self._review) and not bool(self._activity or self._task))
        if selected.current and result.get("reason") and not self._activity:
            hint += " " + REASONS.get(result["reason"], result["reason"])
        if self._activity:
            hint += " 处理中的阶段结果在保存后可查看。"
        self.stage_hint.setText(selected.title + " · " + STATE_LABELS[selected.state] + "\n" + hint)
        allowed = {
            "intake": {"export"},
            "parsing": {"export", "ocr", "reparse", "continue"},
            "facts": {"continue"},
            "planning": {"continue"},
            "generation": {"continue"},
            "publication": {"continue", "navigation"},
        }[self._selected_stage] | {"logs", "stop"}
        for key, button in self.stage_buttons.items():
            button.setVisible(key in allowed and (key != "stop" or self._activity is not None))
            available = self._saved is not None
            if key in {"continue", "reparse", "navigation"}:
                available &= not bool(self._activity or self._task)
            if key == "continue":
                available &= (
                    result.get("knowledge_compilation") != "completed"
                    or bool(result.get("omissions"))
                ) and result.get("reason") != "needs_acceptance"
            if key == "navigation":
                available &= bool(result.get("parse_id"))
            if key == "stop":
                available &= bool(self._activity and self._activity.state != "stopping")
            button.setEnabled(available)

    def show_stage(self, key, *, load=True):
        if key not in {stage[0] for stage in STAGES}:
            return
        self._selected_stage = key
        self._artifact_request += 1
        visible = {
            "intake": (0,),
            "parsing": (1, 2),
            "facts": (5,),
            "planning": (5,),
            "generation": (5,),
            "publication": (6, 3, 4, 0),
        }[key]
        if key == "parsing" and ((self._saved or {}).get("source") or {}).get("suffix") != ".pdf":
            visible = (1,)
        self.tabs.setTabVisible(visible[0], True)
        self.tabs.setCurrentIndex(visible[0])
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, index in visible)
        self.refresh_flow()
        if not load:
            self.record_status.setText(
                "正在读取资料与阶段记录，处理期间可先查看流程位置和任务日志。"
            )
            return
        if key == "parsing":
            self.load_parse(0)
        elif key in {"facts", "planning", "generation"}:
            self.load_artifacts(0)
        elif key == "publication":
            self.load_published()
            self.load_navigation(0)
            if ((self._saved or {}).get("result") or {}).get("reason") == "needs_acceptance":
                self.tabs.setCurrentIndex(3)

    def load_artifacts(self, offset):
        from openkb.application.source_artifacts import compilation_artifacts

        if offset is None:
            return
        self._artifact_request += 1
        revision = self._artifact_request
        self.records.clear()
        self.record_text.clear()
        for widget in (self.records, self.record_previous, self.record_next, self.record_text):
            widget.hide()
        self.record_empty_space.show()
        self.record_previous.setEnabled(False)
        self.record_next.setEnabled(False)
        saved = self._saved or {}
        result = saved.get("result") or {}
        if not result.get("parse_id"):
            self.record_status.setText("暂无已保存的解析结果；此阶段尚无可读取的内容。")
            return
        self.record_status.setText(
            "正在读取已保存结果…运行期间等待资料释放读取锁。"
            if self._activity
            else "正在读取已保存结果…"
        )
        stage, source = self._selected_stage, saved["source"]

        def loaded(value):
            if revision != self._artifact_request:
                return
            self._artifact_offset, self._artifact_next = offset, value["next_offset"]
            self.record_status.setText(
                f"本解析版本保存了 {value['total']} 份阶段记录（含历史处理轮次）。"
                "记录数不代表完整覆盖或已入库。"
                if value["total"]
                else "此阶段暂无已保存结果。继续处理会从实际未完成的位置恢复。"
            )
            for widget in (self.records, self.record_previous, self.record_next, self.record_text):
                widget.setVisible(bool(value["total"]))
            self.record_empty_space.setVisible(not value["total"])
            for index, record in enumerate(value["records"], offset + 1):
                self.records.addItem(
                    f"第 {index} 组 · {'待校验草稿' if record['draft'] else '已保存结果'}",
                    record,
                )
            self.record_previous.setEnabled(offset > 0)
            self.record_next.setEnabled(value["next_offset"] is not None)

        self.read(
            lambda: compilation_artifacts(
                self.kb, self.source_id, source["id"], result["parse_id"], stage, offset=offset
            ),
            loaded,
        )

    def display_record(self, *_):
        record = self.records.currentData()
        if record:
            self.record_text.setPlainText(
                record["text"]
                + ("\n\n［此记录仅显示前 16,000 字符］" if record["truncated"] else "")
            )

    def load_published(self):
        from openkb.application.source_artifacts import published_source_pages

        self.published_pages.clear()
        self.open_published.setEnabled(False)
        if not self._saved:
            return
        version = self._saved["source"]["id"]

        def loaded(pages):
            for path in pages:
                self.published_pages.addItem(path, path)
            self.published_status.setText(
                f"本资料版本已有 {len(pages)} 个知识页面。"
                if pages
                else "本资料版本尚无已入库的知识页面。草稿和待接受差异可在其他阶段查看。"
            )
            self.open_published.setEnabled(bool(pages))

        self.read(lambda: published_source_pages(self.kb, self.source_id, version), loaded)

    def open_published_page(self):
        path = self.published_pages.currentData()
        if path and getattr(self.window, "kb", self.kb) == self.kb:
            self.window.open_page(path)
            self.done(1)

    def source_task(self):
        if self._activity:
            return self.window.manager.get(self._activity.task_id)
        version = self._saved["source"]["id"] if self._saved else None
        return next(
            (
                task
                for task in reversed(self.window.manager.tasks())
                if Path(task.kb_dir) == self.kb
                and any(
                    row.document
                    and row.document.source_id == self.source_id
                    and row.document.input_version == version
                    for row in task.results
                )
            ),
            None,
        )

    def show_source_task(self):
        from openkb.desktop.task_details import show_task_details

        task = self.source_task()
        if task:
            show_task_details(task, self, manager=self.window.manager)
        else:
            self.status.setText("当前程序未保留此资料的任务日志；保存的处理结果仍可查看。")

    def stop_source_task(self):
        if self._activity:
            self.window.manager.stop(self._activity.task_id)
            self.status.setText("已请求安全停止当前任务；已保存内容可在之后继续处理。")
