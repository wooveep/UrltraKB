"""Read retained originals, evidence and proposed changes for one source."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.application.source_actions import (
    inspect_source_parse,
    read_source_evidence,
    review_source_proposal,
    source_page_image,
)
from openkb.application.source_history import source_status
from openkb.desktop.source_presentation import (
    KINDS,
    evidence_text,
    position_text,
    quality_text,
    status_text,
)
from openkb.evidence import Evidence
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import (
    CleanupSourceHistory,
    ConfirmSourcePage,
    ContinueSource,
    ReparseSource,
    ReprocessSourcePage,
)


class SourceReview(QDialog):
    def __init__(self, window, kb, source_id):
        super().__init__(window)
        self.window, self.kb, self.source_id = window, kb, source_id
        self._closed, self._generation = False, 0
        self._saved = self._review = self._task = None
        self._next = None
        self._offset = 0
        self.setWindowTitle("资料原文与处理结果")
        self.resize(940, 760)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取资料…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        for label, callback in (
            ("刷新", self.reload),
            ("导出原文…", self.export),
            ("继续处理", self.continue_saved),
            ("重新解析", self.reparse),
            ("清理本库历史…", self.cleanup_history),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        tabs = QTabWidget()
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        tabs.addTab(self.details, "处理状态")
        evidence = QWidget()
        body = QVBoxLayout(evidence)
        self.blocks = QComboBox()
        self.blocks.setAccessibleName("选择原文片段")
        self.blocks.activated.connect(self.read_block)
        body.addWidget(self.blocks)
        pages = QHBoxLayout()
        previous = QPushButton("上一组片段")
        previous.clicked.connect(lambda: self.load_parse(max(0, self._offset - 100)))
        following = QPushButton("下一组片段")
        following.clicked.connect(lambda: self.load_parse(self._offset + 100))
        next_span = QPushButton("继续读取当前片段")
        next_span.clicked.connect(self.read_next)
        for button in (previous, following, next_span):
            pages.addWidget(button)
        body.addLayout(pages)
        self.content = QPlainTextEdit()
        self.content.setReadOnly(True)
        body.addWidget(self.content)
        tabs.addTab(evidence, "原文证据")
        original = QWidget()
        visual = QVBoxLayout(original)
        controls = QHBoxLayout()
        self.page_number = QSpinBox()
        self.page_number.setRange(1, 1_000_000)
        self.page_number.setPrefix("原文物理页：")
        controls.addWidget(self.page_number)
        self._viewed_page = None
        preview = QPushButton("查看原页")
        preview.clicked.connect(self.preview_page)
        controls.addWidget(preview)
        for label, reason in (
            ("确认为合理空白", "legitimate_blank"),
            ("确认为合理插图", "legitimate_illustration"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, selected=reason: self.confirm_page(selected)
            )
            controls.addWidget(button)
        visual.addLayout(controls)
        retry = QPushButton("重新识别当前页")
        retry.clicked.connect(self.reprocess_page)
        visual.addWidget(retry)
        scroll = QScrollArea()
        self.page_image = QLabel("PDF 资料可在这里逐页检查原文。DOCX 请导出原文查看。")
        scroll.setWidget(self.page_image)
        visual.addWidget(scroll)
        tabs.addTab(original, "逐页检查")
        proposed = QWidget()
        plan = QVBoxLayout(proposed)
        buttons = QHBoxLayout()
        review = QPushButton("查看待接受的差异")
        review.clicked.connect(self.review)
        self.accept = QPushButton("接受以上列出的页面差异")
        self.accept.setEnabled(False)
        self.accept.clicked.connect(self.accept_review)
        buttons.addWidget(review)
        buttons.addWidget(self.accept)
        plan.addLayout(buttons)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        plan.addWidget(self.diff)
        tabs.addTab(proposed, "知识变更")
        layout.addWidget(tabs)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.reload()

    def read(self, function, callback):
        generation = self._generation

        def loaded(value, error):
            if self._closed or generation != self._generation:
                return
            if error:
                self.status.setText(f"读取失败（{type(error).__name__}），请刷新后重试。")
                return
            callback(value)

        self.window.io.submit(
            function,
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def reload(self):
        self._generation += 1
        self._review = None
        self._viewed_page = None
        self.accept.setEnabled(False)

        def loaded(value):
            self._saved = value
            result = value["result"] or {}
            labels = {
                "completed": "知识编译完成",
                "unfinished": "知识编译未完成",
                "failed": "知识编译失败",
                "stopped": "知识编译已停止",
            }
            self.status.setText(
                value["source"]["name"]
                + " · 原文已保存 · "
                + labels.get(result.get("knowledge_compilation"), "等待知识编译")
            )
            self.details.setPlainText(status_text(value))
            self.load_parse(0)

        self.read(lambda: source_status(self.kb, self.source_id), loaded)

    def load_parse(self, offset):
        if not self._saved or not (result := self._saved["result"]) or not result.get("parse_id"):
            self.content.setPlainText("暂无可读取的解析结果，原文仍已保存。")
            return

        def loaded(value):
            self._offset = offset
            self.blocks.clear()
            for block in value["blocks"]:
                position = position_text(block["location"])
                kind = KINDS.get(block["kind"], block["kind"])
                self.blocks.addItem(f"{block['order'] + 1}. {kind} · {position}", block)
            self.content.setPlainText(quality_text(value["quality"]))

        self.read(
            lambda: inspect_source_parse(
                self.kb,
                self.source_id,
                version_id=self._saved["source"]["id"],
                parse_id=result["parse_id"],
                offset=offset,
            ),
            loaded,
        )

    def read_block(self, *_):
        block = self.blocks.currentData()
        if block is None or self._saved is None:
            return
        self._next = Evidence(
            self.source_id,
            self._saved["source"]["id"],
            self._saved["result"]["parse_id"],
            block["id"],
        )
        self.read_next()

    def read_next(self):
        from dataclasses import replace

        reference = self._next
        if reference is None:
            return

        def loaded(value):
            self.content.setPlainText(evidence_text(value))
            self._next = replace(reference, start=value.next_start) if value.next_start else None

        self.read(lambda: read_source_evidence(self.kb, reference, max_chars=16_000), loaded)

    def review(self):
        result = self._saved["result"] if self._saved else None
        if not result or result.get("reason") != "needs_acceptance":
            self.status.setText("当前没有等待接受的页面差异。")
            return

        def loaded(value):
            self._review = value
            self.diff.setPlainText(
                "需要明确接受的页面：\n"
                + "\n".join(value["protected"])
                + "\n\n"
                + "\n".join(value["diffs"].values())
            )
            self.accept.setEnabled(True)

        self.read(
            lambda: review_source_proposal(self.kb, result["resume"], max_chars=1_000_000), loaded
        )

    def continue_saved(self):
        if self._saved and self._task is None:
            result = self._saved["result"] or {}
            if result.get("reason") == "needs_acceptance":
                self.status.setText("请在“知识变更”中查看差异并接受列出的页面。")
                return
            self.submit(ContinueSource(self.source_id, self._saved["source"]["id"]))

    def accept_review(self):
        if self._review and self._task is None:
            self.submit(
                ContinueSource(
                    self.source_id,
                    self._review["version_id"],
                    self._review["id"],
                    tuple(self._review["protected"]),
                )
            )

    def reparse(self):
        if self._saved and self._task is None:
            self.submit(ReparseSource(self.source_id, self._saved["source"]["id"]))

    def preview_page(self):
        if not self._saved:
            return
        page, version_id = self.page_number.value(), self._saved["source"]["id"]

        def loaded(value):
            picture = QPixmap()
            if not picture.loadFromData(value, "PNG"):
                self.status.setText("原页图像无法读取。")
                return
            self.page_image.setPixmap(picture)
            self.page_image.resize(picture.size())
            self._viewed_page = (version_id, page)

        self.read(
            lambda: source_page_image(self.kb, self.source_id, version_id=version_id, page=page),
            loaded,
        )

    def confirm_page(self, reason):
        if not self._saved or self._task:
            return
        result = self._saved["result"] or {}
        version_id, page = self._saved["source"]["id"], self.page_number.value()
        if self._viewed_page != (version_id, page) or not result.get("parse_id"):
            self.status.setText("请先查看本版本对应的原文物理页，再记录检查结论。")
            return
        label = "合理空白" if reason == "legitimate_blank" else "合理插图"
        if (
            QMessageBox.question(
                self,
                "记录原页检查结论",
                f"已检查原文第 {page} 页，确认为{label}？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.submit(
                ConfirmSourcePage(self.source_id, version_id, result["parse_id"], page, reason)
            )

    def reprocess_page(self):
        if not self._saved or self._task:
            return
        result = self._saved["result"] or {}
        version_id, page = self._saved["source"]["id"], self.page_number.value()
        if self._viewed_page != (version_id, page) or not result.get("parse_id"):
            self.status.setText("请先查看当前版本的原文物理页。")
            return
        unknown = any(
            job.get("page") == page
            and job.get("version_id") == version_id
            and job.get("state") in {"submitting", "submission_unknown"}
            for job in self._saved.get("cloud_jobs", [])
        )
        message = f"使用当前 OCR 设置重新识别第 {page} 页，保留旧解析与证据。"
        if unknown:
            message += "\n先前云提交的结果不明。新提交可能重复处理并产生额外费用，是否继续？"
        else:
            message += "是否继续？"
        if (
            QMessageBox.question(
                self,
                "重新识别当前页",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.submit(
                ReprocessSourcePage(self.source_id, version_id, result["parse_id"], page, unknown)
            )

    def cleanup_history(self):
        from openkb.application.source_cleanup import preview_history_cleanup

        if self._task:
            return

        def loaded(preview):
            if not preview.files:
                self.status.setText("没有可清理的历史。当前资料、知识引用与 OCR 恢复检查点均保留。")
                return
            review = QMessageBox(self)
            review.setWindowTitle("清理本库未引用历史")
            review.setText(
                f"将清理 {len(preview.versions)} 个旧资料版本、{len(preview.parses)} 个旧解析，"
                f"共 {len(preview.files)} 个文件（{preview.bytes:,} 字节）。\n"
                "当前版本、知识引用、OCR 恢复检查点与累计用量记录保留。清理后这些历史文件无法恢复。"
            )
            review.setDetailedText("\n".join(preview.files))
            review.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            review.setDefaultButton(QMessageBox.StandardButton.No)
            if review.exec() == QMessageBox.StandardButton.Yes:
                self.submit(CleanupSourceHistory(preview.id))

        self.read(lambda: preview_history_cleanup(self.kb), loaded)

    def submit(self, request):
        self._task = self.window.manager.submit(self.kb, [request])
        self.accept.setEnabled(False)
        self.status.setText("处理任务已提交，可在任务页查看进度或停止。")

    def export(self):
        if not self._saved:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出已保存的原文", self._saved["source"]["name"]
        )
        if not path:
            return
        version_id = self._saved["source"]["id"]

        def copy():
            from openkb.mutation import _copy_file_atomic
            from openkb.sources import SourceStore

            target = Path(path).absolute()
            if target.resolve().is_relative_to(self.kb.resolve()) or target.is_symlink():
                raise ValueError("Export outside the knowledge base")
            store = SourceStore(self.kb)
            _copy_file_atomic(store.original(store.version(version_id)), target)

        self.read(copy, lambda _: self.status.setText("原文已导出。"))

    def poll(self):
        if self._task and self.window.manager.get(self._task).state in TERMINAL:
            self._task = None
            self.reload()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
