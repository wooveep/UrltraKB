"""Small completed-answer actions derived from the actual native document text."""

import unicodedata

from PySide6.QtCore import Qt, QTextBoundaryFinder
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from ultrakb.desktop.flow_layout import FlowLayout

COUNT_POLICY = (
    "按实际显示的正文文字计数，包含标点、代码与表格；不计空白和 Markdown 格式标记。"
    "组合字符和表情按一个可见字符簇计算，例如 é、👨‍👩‍👧‍👦、🇨🇳 各为 1。"
    "图形公式和图片本身不计；若公式源码或图片替代文字实际显示为文本，则按该文字计数。"
    "链接只计显示名称；字符数不等于词数或 token 用量。"
)


def character_count(text):
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
    raw, start, count, single_regional = text.encode("utf-16-le"), 0, 0, False
    while (end := finder.toNextBoundary()) != -1:
        cluster = raw[start * 2 : end * 2].decode("utf-16-le")
        # Qt splits regional indicators; GB12/13 of Unicode UAX #29 pair them.
        regional = len(cluster) == 1 and 0x1F1E6 <= ord(cluster) <= 0x1F1FF
        if any(
            not char.isspace()
            and char != "\ufffc"
            and unicodedata.category(char) not in {"Cf", "Cc"}
            for char in cluster
        ):
            if not regional or not single_regional:
                count += 1
        single_regional = regional and not single_regional
        start = end
    return count


class AnswerActions(QWidget):
    def __init__(self, reader, data, parent):
        super().__init__(parent)
        self.reader, self.data = reader, data
        self.clipboard = QApplication.clipboard
        self.setObjectName("answerActions")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        row = FlowLayout()
        layout.addLayout(row)
        self.copy = QPushButton("复制")
        self.copy.setFlat(True)
        self.copy.setAccessibleName("复制此回答 Markdown")
        self.copy.clicked.connect(self.copy_answer)
        row.addWidget(self.copy)
        self.statistics = QPushButton()
        self.statistics.setFlat(True)
        self.statistics.setAccessibleName("查看此回答字符统计")
        self.statistics.clicked.connect(self.show_statistics)
        row.addWidget(self.statistics)
        self.feedback = QLabel()
        self.feedback.setObjectName("muted")
        self.feedback.setWordWrap(True)
        row.addWidget(self.feedback)
        from ultrakb.desktop.answer_sources import CitationControls

        self.sources = CitationControls(self)
        row.addWidget(self.sources.toggle)
        layout.addWidget(self.sources)
        self.count = character_count(reader.document().toPlainText())
        self.statistics.setText(f"{self.count:,} 字符")
        self.setStyleSheet(
            "QPushButton { border: 0; background: transparent; padding: 4px 8px; "
            "min-height: 28px; }"
            "QPushButton:hover, QPushButton:focus { border: 1px solid palette(highlight); "
            "border-radius: 6px; }"
        )

    def copy_answer(self):
        try:
            clipboard = self.clipboard()
            clipboard.setText(self.data.markdown)
            if clipboard.text() != self.data.markdown:
                raise RuntimeError("Clipboard did not accept this answer")
        except Exception:
            self.feedback.setText("复制失败，请重试")
        else:
            self.feedback.setText("已复制")

    def show_statistics(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("本条回答统计")
        dialog.resize(560, 320)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"本条回答：{self.count:,} 字符")
        layout.addWidget(title)
        policy = QLabel(COUNT_POLICY)
        policy.setWordWrap(True)
        layout.addWidget(policy)
        usage = self.data.usage
        if usage is None:
            value = "未提供单条模型用量；不以会话累计值替代。"
        else:
            values = [
                ("输入命中缓存", "input_cached"),
                ("输入未命中缓存", "input_uncached"),
                ("输出", "output"),
                ("思考", "reasoning"),
            ]
            value = "本条已报告的模型用量（tokens）：\n" + "\n".join(
                f"{label}：{usage.get(key) if usage.get(key) is not None else '未提供'}"
                for label, key in values
            )
            value += "\n输出已包含思考，不重复相加；包括本次回答的工具调用期间请求。"
        details = QLabel(value)
        details.setWordWrap(True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(details)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
