"""Read retained original-format artifacts without exposing compilation internals."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

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
            limit = decision["pdf_limit"]
            policy = QLabel(
                f"分类：{classification} · 执行：{execution} · 容量：{capacity}\n"
                f"导入时短 PDF 上限：{limit['short_max_pages']} 页；"
                f"来源：{limit['source']} / {limit['key']}"
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
        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        if source.get("units"):
            controls = QHBoxLayout()
            self.pages = QLineEdit()
            self.pages.setAccessibleName("来源页范围")
            self.pages.setPlaceholderText("物理页，如 1,3-5；留空显示全文")
            select = QPushButton("读取页范围")
            select.setAutoDefault(False)
            select.clicked.connect(self.select_pages)
            self.pages.returnPressed.connect(self.select_pages)
            controls.addWidget(self.pages, 1)
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
        self.reader.show_markdown(
            source["content"],
            self.kb / (self.source.get("base_path") or "wiki/sources"),
            dark=self.window.appearance.dark,
            scale=self.window.zoom.currentData(),
        )

    def select_pages(self):
        from openkb.source_pages import read_page_selection

        try:
            selected = read_page_selection(self.source["units"], self.pages.text().strip() or None)
        except ValueError as exc:
            self.coverage.setText(str(exc))
            return
        self.show_content(selected)

    def closeEvent(self, event):
        self.reader.stop_rendering()
        super().closeEvent(event)


def show_source(window, kb, source):
    dialog = SourceReader(window, kb, source)
    dialog.show()
    return dialog
