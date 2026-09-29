"""Creation brief and library composition for the artifact workbench."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.desktop.flow_layout import FlowLayout
from openkb.desktop.fonts import text_font
from openkb.desktop.reader import MarkdownView


def label(text, name="muted"):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setObjectName(name)
    return widget


def build_workspace(panel):
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(12)
    header = QHBoxLayout()
    header.addWidget(label("创作工作台", "sectionTitle"), 1)
    panel.new_button = QPushButton("新建创作")
    panel.new_button.setObjectName("primaryAction")
    panel.new_button.setCheckable(True)
    header.addWidget(panel.new_button)
    layout.addLayout(header)
    layout.addWidget(
        label("把已有知识变成可使用、可分享的成果。选择目标 → 描述要求 → 预览与导出。")
    )

    panel.brief = QFrame()
    panel.brief.setObjectName("overviewCard")
    brief = QVBoxLayout(panel.brief)
    form = QFormLayout()
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    panel.kind = QComboBox()
    panel.kind.addItem("可复用技能 · Skill", "skill")
    panel.kind.addItem("演示文稿 · HTML 幻灯片", "deck")
    form.addRow("创作目标", panel.kind)
    panel.kind_hint = label("")
    form.addRow("", panel.kind_hint)
    panel.name = QLineEdit()
    panel.name.setPlaceholderText("例如 attention-guide（英文、数字或短横线）")
    form.addRow("成果名称", panel.name)
    panel.intent = QPlainTextEdit()
    panel.intent.setPlaceholderText("面向谁？希望解决什么问题？需要包含哪些知识？")
    panel.intent.setMaximumHeight(80)
    form.addRow("创作要求", panel.intent)
    brief.addLayout(form)
    panel.generate_button = QPushButton("开始生成")
    panel.generate_button.setObjectName("primaryAction")
    panel.generate_button.clicked.connect(panel.generate)
    brief.addWidget(panel.generate_button, 0, Qt.AlignmentFlag.AlignRight)
    panel.kind.currentIndexChanged.connect(panel.describe_kind)
    panel.new_button.toggled.connect(panel.brief.setVisible)
    panel.brief.hide()
    panel.describe_kind()
    layout.addWidget(panel.brief)

    tools = FlowLayout()
    tools.addWidget(label("探索知识关联"))
    panel.graph_button = QPushButton("生成知识图谱")
    panel.graph_button.setToolTip("根据知识之间的链接生成交互图谱，完成后可在浏览器查看。")
    panel.graph_button.clicked.connect(panel.graph)
    tools.addWidget(panel.graph_button)
    tasks = QPushButton("查看任务进度")
    tasks.clicked.connect(lambda: panel.window.shell.navigate("任务"))
    tools.addWidget(tasks)
    layout.addLayout(tools)

    panel.status = label("选择已有成果，或新建一次创作。生成任务可在后台继续。")
    layout.addWidget(panel.status)
    library_header = QHBoxLayout()
    panel.library_count = label("我的成果", "sectionTitle")
    library_header.addWidget(panel.library_count, 1)
    refresh = QPushButton("刷新")
    refresh.clicked.connect(panel.refresh)
    library_header.addWidget(refresh)
    layout.addLayout(library_header)
    library = QWidget()
    left = QVBoxLayout(library)
    left.setContentsMargins(0, 0, 0, 0)
    panel.search = QLineEdit()
    panel.search.setPlaceholderText("搜索成果…")
    panel.search.setAccessibleName("搜索成果")
    panel.search.setClearButtonEnabled(True)
    panel.search.textChanged.connect(panel.filter_items)
    left.addWidget(panel.search)
    panel.filter_kind = QComboBox()
    panel.filter_kind.addItems(
        ["全部成果", "Skill", "幻灯片", "知识图谱", "探索笔记", "检查报告", "文件"]
    )
    panel.filter_kind.setAccessibleName("成果类型")
    panel.filter_kind.currentIndexChanged.connect(panel.filter_items)
    left.addWidget(panel.filter_kind)
    panel.items = QListWidget()
    panel.items.setMinimumWidth(160)
    panel.items.currentItemChanged.connect(panel.select)
    left.addWidget(panel.items, 1)
    panel.empty_hint = label("还没有成果。点击「新建创作」开始。")
    left.addWidget(panel.empty_hint)

    content = QWidget()
    right = QVBoxLayout(content)
    right.setContentsMargins(8, 0, 0, 0)
    panel.artifact_title = label("选择一个成果", "sectionTitle")
    right.addWidget(panel.artifact_title)
    panel.artifact_hint = label("在这里阅读成果，预览演示文稿，或导出完整文件包。")
    right.addWidget(panel.artifact_hint)
    panel.files = QComboBox()
    panel.files.setAccessibleName("成果文件")
    panel.files.hide()
    panel.files.currentIndexChanged.connect(panel.read)
    right.addWidget(panel.files)
    panel.reader = MarkdownView()
    panel.source = QPlainTextEdit()
    panel.source.setReadOnly(True)
    panel.source.setFont(text_font(14, code=True))
    panel.results = QPlainTextEdit()
    panel.results.setReadOnly(True)
    panel.tabs = QTabWidget()
    panel.tabs.addTab(panel.reader, "内容预览")
    panel.tabs.addTab(panel.source, "源文件")
    panel.tabs.addTab(panel.results, "任务记录")
    right.addWidget(panel.tabs, 1)
    actions = FlowLayout()
    panel.preview_button = QPushButton("在浏览器预览")
    panel.preview_button.setObjectName("primaryAction")
    panel.preview_button.setEnabled(False)
    panel.preview_button.clicked.connect(panel.preview)
    panel.export_button = QPushButton("导出完整成果…")
    panel.export_button.setEnabled(False)
    panel.export_button.clicked.connect(panel.export)
    actions.addWidget(panel.preview_button)
    actions.addWidget(panel.export_button)
    right.addLayout(actions)
    splitter = QSplitter()
    splitter.addWidget(library)
    splitter.addWidget(content)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([230, 700])
    layout.addWidget(splitter, 1)
