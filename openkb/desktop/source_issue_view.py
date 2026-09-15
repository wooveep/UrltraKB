"""Read-only pending-original view for the generation and verification stage."""

from dataclasses import replace

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

from openkb.application.source_actions import read_source_evidence
from openkb.application.source_issues import source_issues
from openkb.desktop.source_presentation import REASONS, position_text
from openkb.evidence import Evidence

_REASONS = {
    "analysis_pending": "这段原文尚未完成分析。",
    "knowledge_content_omitted": "这段原文仍有内容未进入已验证知识。",
    "image_understanding_pending": "图像含义仍待分析；已保留原图。",
}


class SourceIssueView(QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._revision = 0
        self._offset, self._next_offset, self._reference = 0, None, None
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取待处理原文…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.items = QComboBox()
        self.items.setAccessibleName("选择待处理原文或排除项目")
        self.items.currentIndexChanged.connect(self.select)
        layout.addWidget(self.items)
        controls = QHBoxLayout()
        self.previous = QPushButton("上一组")
        self.next = QPushButton("下一组")
        self.more = QPushButton("继续读取此段原文")
        self.original = QPushButton("查看原文与解析")
        self.previous.clicked.connect(lambda: self.load(max(0, self._offset - 50)))
        self.next.clicked.connect(lambda: self.load(self._next_offset))
        self.more.clicked.connect(self.read_more)
        self.original.clicked.connect(self.open_original)
        for button in (self.previous, self.next, self.more, self.original):
            button.setAutoDefault(False)
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.reason = QLabel()
        self.reason.setTextFormat(Qt.TextFormat.PlainText)
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName("待处理的原文内容")
        layout.addWidget(self.text, 1)

    def cancel(self):
        self._revision += 1
        self._reference = None

    def load(self, offset=0):
        if offset is None:
            return
        self.cancel()
        self.items.clear()
        revision = self._revision
        self.text.clear()
        self.reason.clear()
        for button in (self.previous, self.next, self.more):
            button.setEnabled(False)
        saved = self.panel._saved or {}
        source, result = saved.get("source", {}), saved.get("result") or {}
        if not result.get("parse_id"):
            self.status.setText("尚无已保存的解析结果，暂时无法定位待处理原文。")
            return
        self.status.setText("正在读取待处理原文…")

        def loaded(value):
            if revision != self._revision:
                return
            self._offset, self._next_offset = offset, value["next_offset"]
            self.status.setText(
                f"已排除 {value['excluded']} 项 · 待处理原文 {value['pending']} 段。"
                "排除项可能涉及多段原文；排除不代表原文有错。"
                if value["total"]
                else "没有记录到待处理或排除的内容。"
            )
            if not value["coverage_known"]:
                self.status.setText(self.status.text() + " 此历史记录未保存原文覆盖范围。")
            self.previous.setEnabled(offset > 0)
            self.next.setEnabled(value["next_offset"] is not None)
            for index, row in enumerate(value["rows"], offset + 1):
                self.items.addItem(
                    f"{index}. {position_text(row['location'])}"
                    + (" · " + row["item"] if row["item"] else ""),
                    row,
                )

        self.panel.read(
            lambda: source_issues(
                self.panel.kb,
                self.panel.source_id,
                source["id"],
                result["parse_id"],
                result.get("coverage") or {},
                result.get("omissions") or (),
                offset=offset,
            ),
            loaded,
        )

    def select(self, *_):
        self.cancel()
        self.text.clear()
        self.more.setEnabled(False)
        row = self.items.currentData()
        if not row:
            return
        reason = _REASONS.get(row["reason"], REASONS.get(row["reason"], row["reason"]))
        stage = {
            "facts": "事实提取",
            "planning": "主题规划",
            "generation": "生成与校验",
            "parsing": "内容解析",
        }.get(row["stage"], "原文覆盖")
        self.reason.setText(stage + " · " + reason.partition("，本轮知识")[0])
        if row["reference"]:
            self._reference = Evidence(**row["reference"])
            self.read_more()
        else:
            self.text.setPlainText(
                "此记录没有保存可精确定位的原文范围。\n"
                "可在列表中查看其他待处理片段，或打开“查看原文与解析”。\n\n"
                + ("记录项目：" + row["item"] if row["item"] else "")
            )

    def read_more(self):
        reference = self._reference
        if reference is None:
            return
        self._revision += 1
        revision = self._revision
        self.more.setEnabled(False)

        def loaded(value):
            if revision != self._revision:
                return
            body = value.text or "此位置没有可显示的解析文本。请在原文与解析页检查原图。"
            if value.assets:
                body += "\n\n此位置还包含图像，可在原文与解析页检查。"
            self.text.setPlainText(position_text(value.location) + "\n\n原文内容：\n" + body)
            self._reference = (
                replace(reference, start=value.next_start) if value.next_start is not None else None
            )
            self.more.setEnabled(self._reference is not None)

        self.panel.read(
            lambda: read_source_evidence(self.panel.kb, reference, max_chars=16_000), loaded
        )

    def open_original(self):
        row = self.items.currentData() or {}
        self.panel.show_stage("parsing")
        source = (self.panel._saved or {}).get("source", {})
        page = row.get("location", {}).get("page")
        if source.get("suffix") == ".pdf" and page:
            self.panel.page_number.setValue(page)
            self.panel.tabs.setCurrentIndex(2)
            self.panel.preview_page()
        elif row.get("reference"):
            self.panel._next = Evidence(**row["reference"])
            self.panel.read_next()
