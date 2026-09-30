"""Read retained original-format artifacts without exposing compilation internals."""

import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.desktop.reader import MarkdownView


class SourceReader(QDialog):
    def __init__(self, window, kb, source):
        super().__init__(window)
        self.setWindowTitle(f"原文 · {source['name']}")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 720)
        self.window, self.kb, self.source = window, kb, source
        layout = QVBoxLayout(self)
        hint = QLabel("导入时保留的原文 · 摘要、概念和实体请在知识页阅读")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        if source.get("processing"):
            decision = source["processing"]
            classification = {"short": "短文", "long": "长文"}[decision["length_class"]]
            execution = {"full": "全文编译", "segmented": "分段编译"}[decision["execution_mode"]]
            capacity = {
                "unknown": "未知，尝试全文",
                "insufficient": "不足，使用分段",
                "sufficient": "可容纳首次全文请求",
                "not_needed": "长文直接分段",
            }[decision["capacity_status"]]
            limit = decision.get("pdf_limit")
            measured = (
                f"导入时短 PDF 上限：{limit['short_max_pages']} 页；"
                f"来源：{limit['source']} / {limit['key']}"
                if limit
                else f"文本：{decision['measurement_value']} tokens · cl100k_base；短文上限 5,000"
            )
            policy = QLabel(
                f"分类：{classification} · 执行：{execution} · 容量：{capacity}\n" + measured
            )
            policy.setWordWrap(True)
            layout.addWidget(policy)
        target = source.get("target_processing")
        if target and target != source.get("processing"):
            pending = QLabel(
                f"新目标处理：{target['length_class']} / {target['execution_mode']}；"
                f"容量：{target['capacity_status']}。当前正文仍采用上次成功结果。"
            )
            pending.setWordWrap(True)
            layout.addWidget(pending)
        if source.get("source_revision_id"):
            metadata = source.get("version_metadata", {})
            target_revision = (
                source.get("target_source_revision_id") or source["source_revision_id"]
            )
            revision = QLabel(
                f"产品：{metadata.get('product') or '未知'} · "
                f"适用版本：{', '.join(metadata.get('applicable_versions', [])) or '未知'} · "
                f"资料修订：{metadata.get('document_revision') or '未提供'}\n"
                f"正文依据：{source['source_revision_id']}\n"
                f"知识修订：{source.get('knowledge_revision_id') or '尚未发布'}\n"
                f"目标修订：{target_revision}\n"
                f"依据有效性：{source.get('validity', 'current')}\n"
                f"处理状态：{source.get('status', '')}\n{source.get('message') or ''}"
            )
            revision.setWordWrap(True)
            layout.addWidget(revision)
        if source.get("original_path"):
            original = QPushButton(
                "打开旧库来源快照"
                if source.get("original_kind") == "legacy_snapshot"
                else "打开冻结原件"
            )
            original.setAutoDefault(False)
            original.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(kb / source["original_path"]))
                )
            )
            layout.addWidget(original)
        if source.get("internal_pdf_path"):
            internal_pdf = QPushButton("打开导入时生成的 PDF")
            internal_pdf.setAutoDefault(False)
            internal_pdf.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(kb / source["internal_pdf_path"]))
                )
            )
            layout.addWidget(internal_pdf)
            office = source["office"]
            substitutions = ", ".join(
                f"{item['requested']} → {item['actual']}" for item in office["font_substitutions"]
            )
            conversion = QLabel(
                f"LibreOffice {office['version']} · PDF {office['pages']} 页\n"
                + "\n".join(office["diagnostics"])
                + (f"\n已核对的字体替换：{substitutions}" if substitutions else "")
            )
            conversion.setWordWrap(True)
            layout.addWidget(conversion)
            if office.get("slides"):
                slides = office["slides"]
                hidden = sum(slide["hidden"] for slide in slides)
                layout.addWidget(
                    QLabel(f"{len(slides)} 张幻灯片，含 {hidden} 张隐藏页；备注不另计页数")
                )
            unresolved = sum(item["status"] != "matched" for item in office["font_observations"])
            if unresolved:
                font_status = QLabel(
                    f"另有 {unresolved} 段字体未能唯一对应到可见 PDF；"
                    "其中可能包含已删除文字，未据此推断字体替换。"
                )
                font_status.setWordWrap(True)
                layout.addWidget(font_status)
        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        if source.get("units"):
            controls = QHBoxLayout()
            self.pages = QLineEdit()
            blocks = source.get("unit_kind") == "block"
            self.pages.setAccessibleName("来源块范围" if blocks else "来源页范围")
            self.pages.setPlaceholderText(
                "内容块，如 1,3-5；留空显示全文" if blocks else "物理页，如 1,3-5；留空显示全文"
            )
            select = QPushButton("读取块范围" if blocks else "读取页范围")
            select.setAutoDefault(False)
            select.clicked.connect(self.select_pages)
            self.pages.returnPressed.connect(self.select_pages)
            controls.addWidget(self.pages, 1)
            if (source.get("office") or {}).get("slides"):
                self.part = QComboBox()
                self.part.setAccessibleName("来源正文或演讲备注")
                for label, value in (
                    ("正文和备注", None),
                    ("仅正文", "body"),
                    ("仅演讲备注", "notes"),
                ):
                    self.part.addItem(label, value)
                controls.addWidget(self.part)
                self.part.currentIndexChanged.connect(self.select_pages)
            controls.addWidget(select)
            layout.addLayout(controls)
        if source.get("unit_kind") in {"text", "block"}:
            controls = QHBoxLayout()
            self.characters = QLineEdit()
            self.characters.setAccessibleName("来源字符范围")
            self.characters.setPlaceholderText(
                "Unicode 字符 START:END，从 0 开始、不含 END；留空显示全文"
            )
            select = QPushButton("读取字符范围")
            select.setAutoDefault(False)
            select.clicked.connect(self.select_characters)
            self.characters.returnPressed.connect(self.select_characters)
            controls.addWidget(self.characters, 1)
            controls.addWidget(select)
            layout.addLayout(controls)
        self.reader = MarkdownView()

        def follow(url):
            if not url.scheme() and url.fragment():
                from openkb.rendering.markdown import heading_anchor

                self.reader.scrollToAnchor(heading_anchor(url.fragment()))
            else:
                window._follow_link(url)

        self.reader.anchorClicked.connect(follow)
        layout.addWidget(self.reader, 1)
        self.show_content(source)

    def show_content(self, source):
        physical = source.get("unit_kind") == "page"
        numbers = source.get("page_range", [])
        self.coverage.setText(
            ("物理页：" if physical else "来源页号（物理对应关系未知）：")
            + (", ".join(map(str, numbers)) or "未记录")
            + (f" · 全文 {source['pages']} 页" if source.get("pages") is not None else "")
            + "\n"
            + "\n".join(source.get("diagnostics", []))
            if self.source.get("units")
            else ""
        )
        if source.get("unit_kind") == "block":
            self.coverage.setText(
                f"内容块：{', '.join(map(str, source.get('block_range') or []))} · "
                f"全文 {source['block_count']} 块 / {source['characters']} 字符 / "
                f"{source['tokens']} tokens\n" + "\n".join(source.get("diagnostics", []))
            )
        if source.get("unit_kind") in {"text", "block"} and source.get("char_range") is not None:
            start, end = source["char_range"]
            self.coverage.setText(
                f"字符范围：[{start}, {end}) · 全文 {source['characters']} 字符 / "
                f"{source['tokens']} tokens\n" + "\n".join(source.get("diagnostics", []))
            )
        content = source["content"]
        if self.source.get("type") == "txt":
            fence = "`" * max(
                3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0)
            )
            content = f"{fence}\n{content}\n{fence}"
        self.reader.show_markdown(
            content,
            self.kb / (self.source.get("base_path") or "wiki/sources"),
            dark=self.window.appearance.dark,
            scale=self.window.zoom.currentData(),
        )
        if source.get("encoding"):
            encoding = source["encoding"]
            basis = {
                "bom": "BOM",
                "utf8": "严格 UTF-8",
                "detected": "自动判断",
                "signature": "XML 字节签名",
                "declaration": "XML 编码声明",
            }[encoding["basis"]]
            self.coverage.setText(
                self.coverage.text() + f"\n原件编码：{encoding['name']}（{basis}）"
            )
        cells = [item["csv"] for item in source.get("origin_locators", []) if item.get("csv")]
        if source.get("resource_policy"):
            from collections import Counter

            policy = source["resource_policy"]
            enabled = "下载并保留" if policy["download_remote_assets"] else "保留链接，不下载"
            basis = {"single": "单次", "kb": "本库", "global": "全局", "default": "默认"}[
                policy["source"]
            ]
            counts = Counter(resource["status"] for resource in source.get("resources", []))
            labels = {
                "retained": "已保留",
                "missing": "未找到",
                "not_requested": "未请求",
                "failed": "获取失败",
            }
            state = " · ".join(f"{labels[key]} {count}" for key, count in counts.items())
            self.coverage.setText(
                self.coverage.text() + f"\nHTML 远程图片：{enabled}（{basis}）\n{state}"
            )
        if cells:
            self.coverage.setText(
                self.coverage.text()
                + "\nCSV 原始记录："
                + _ranges(cell["row"] for cell in cells)
                + "；列："
                + _ranges(cell["column"] for cell in cells)
                + "。行列号从 1 开始，所有值按文本保留。"
            )

    def select_pages(self):
        from openkb.source_pages import read_page_selection, select_page_part

        try:
            if self.source.get("unit_kind") == "block":
                from pageindex.index.utils import parse_pages

                numbers = (
                    parse_pages(self.pages.text().strip())
                    if self.pages.text().strip()
                    else self.source["block_range"]
                )
                if not numbers or set(numbers) - set(self.source["block_range"]):
                    raise ValueError("内容块范围超出已保存原文")
                units = [unit for unit in self.source["units"] if unit["ordinal"] in numbers]
                self.show_content(
                    {
                        **self.source,
                        "content": "".join(unit["content"] for unit in units),
                        "block_range": [unit["ordinal"] for unit in units],
                        "char_range": None,
                        "origin_locators": [
                            origin for unit in units for origin in unit["origin_locators"]
                        ],
                    }
                )
                return
            selected = read_page_selection(self.source["units"], self.pages.text().strip() or None)
            selected = select_page_part(
                selected, self.part.currentData() if hasattr(self, "part") else None
            )
        except ValueError as exc:
            self.coverage.setText(str(exc))
            return
        self.show_content(selected)

    def select_characters(self):
        from openkb.text_source import character_range, clip_text_origins

        try:
            start, end = character_range(
                self.source["content"], self.characters.text().strip() or None
            )
        except ValueError as exc:
            self.coverage.setText(str(exc))
            return
        self.show_content(
            {
                **self.source,
                "content": self.source["content"][start:end],
                "char_range": [start, end],
                "origin_locators": clip_text_origins(
                    self.source.get("origin_locators", []), start, end
                ),
            }
        )

    def closeEvent(self, event):
        self.reader.stop_rendering()
        super().closeEvent(event)


def show_source(window, kb, source):
    dialog = SourceReader(window, kb, source)
    dialog.show()
    return dialog


def _ranges(values):
    intervals = []
    for value in sorted(set(values)):
        if intervals and intervals[-1][1] + 1 == value:
            intervals[-1][1] = value
        else:
            intervals.append([value, value])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in intervals)
