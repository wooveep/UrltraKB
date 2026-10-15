"""Native first-use and new-knowledge-base wizard with a single primary action."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.model_connections import read_connection_state
from ultrakb.desktop.dialog_geometry import resize_to_screen
from ultrakb.desktop.form_controls import FocusComboBox, scroll_form
from ultrakb.desktop.setup_connection import ConnectionSetup, display_model, paragraph

STEPS = ("选择服务商", "连接服务", "选择与测试模型", "设置知识库", "确认创建")


class SetupWizard(QDialog):
    def __init__(self, window, *, kb=None, creating=False):
        super().__init__(window)
        self.setObjectName("setupWizard")
        self.window, self.creating = window, creating
        self._closed = self._creating = self._deferred = False
        self.setWindowTitle("创建知识库" if creating else "连接模型服务")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        resize_to_screen(self, 720, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.progress = QLabel()
        self.progress.setObjectName("eyebrow")
        self.progress.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.progress)
        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.description = paragraph(
            "先连接模型服务，再设置知识库。每一步都可以返回修改。"
            if creating
            else f"配置知识库「{kb.name}」实际使用的连接。"
        )
        layout.addWidget(self.description)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        self.connection = ConnectionSetup(window.io, kb, self)
        self.pages.addWidget(self.connection)
        body.addWidget(self.pages, 1)
        layout.addWidget(scroll_form(content), 1)
        # Keep feedback beside the primary action when the form must scroll.
        for widget in (self.connection.status, self.connection.cancel_test):
            self.connection.layout().removeWidget(widget)
            layout.addWidget(widget)
        self.connection.changed.connect(self.update_controls)
        self.connection.loaded.connect(self.connection_loaded)
        self.connection.verified.connect(self.connection_verified)
        if creating:
            self._knowledge_base_page()
            self._review_page()
            for label in (self.kb_status, self.create_status):
                label.parentWidget().layout().removeWidget(label)
                label.setObjectName("setupStatus")
                layout.addWidget(label)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.later = QPushButton("稍后继续")
        self.later.clicked.connect(self.reject)
        actions.addWidget(self.later)
        actions.addStretch()
        self.back = QPushButton("上一步")
        self.back.clicked.connect(self.go_back)
        actions.addWidget(self.back)
        self.primary = QPushButton("下一步")
        self.primary.setObjectName("primaryAction")
        self.primary.setDefault(True)
        self.primary.clicked.connect(self.advance)
        actions.addWidget(self.primary)
        for button in (self.later, self.back, self.primary):
            button.setMinimumHeight(44)
        layout.addLayout(actions)
        self.skip = QPushButton("稍后连接模型，先创建空知识库")
        self.skip.setMinimumHeight(44)
        self.skip.clicked.connect(self.defer_connection)
        self.skip.setVisible(creating)
        layout.addWidget(self.skip)
        self.update_controls()

    @property
    def step(self):
        return (
            self.connection.step
            if self.pages.currentIndex() == 0
            else self.pages.currentIndex() + 2
        )

    def _knowledge_base_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.connection_summary = paragraph("")
        layout.addWidget(self.connection_summary)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setSpacing(8)
        self.name = QLineEdit("我的知识库")
        self.name.setAccessibleName("知识库名称")
        self.name.setMinimumHeight(44)
        form.addRow("知识库名称", self.name)
        row = QWidget()
        path_layout = QHBoxLayout(row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        self.directory = QLineEdit(str(Path.home() / "UltraKB"))
        self.directory.setAccessibleName("知识库存放文件夹")
        self.directory.setMinimumHeight(44)
        path_layout.addWidget(self.directory, 1)
        browse = QPushButton("选择文件夹")
        browse.setMinimumHeight(44)
        browse.clicked.connect(self.choose_directory)
        path_layout.addWidget(browse)
        form.addRow("存放文件夹（在其中创建知识库）", row)
        self.language = FocusComboBox()
        self.language.setMinimumHeight(44)
        self.language.setAccessibleName("知识内容语言")
        for label, value in (("继承全局默认", None), ("中文", "zh"), ("English", "en")):
            self.language.addItem(label, value)
        form.addRow("知识内容语言", self.language)
        layout.addLayout(form)
        layout.addWidget(
            paragraph("模型连接继承全局默认。高级参数可在创建后进入设置调整。", muted=True)
        )
        self.kb_status = paragraph("")
        self.kb_status.setAccessibleName("知识库创建状态")
        layout.addWidget(self.kb_status)
        layout.addStretch()
        self.pages.addWidget(page)

    def _review_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.review = paragraph("")
        layout.addWidget(self.review)
        layout.addWidget(
            paragraph("创建完成后即可导入资料。知识库将保存原始资料、知识页面和对话。", muted=True)
        )
        self.create_status = paragraph("")
        layout.addWidget(self.create_status)
        layout.addStretch()
        self.pages.addWidget(page)

    def connection_loaded(self, state):
        if self.creating and state.state == "passed":
            self.show_knowledge_base()

    def connection_verified(self, state):
        if self.creating:
            self._deferred = False
            self.show_knowledge_base()
        else:
            self.accept()

    def show_knowledge_base(self):
        state = self.connection.state
        self.connection_summary.setText(
            "模型连接将在首次导入或提问前配置。"
            if self._deferred
            else f"已复用验证通过的连接：{display_model(state.model)}"
        )
        self.pages.setCurrentIndex(1)
        self.update_controls()
        self.name.setFocus()
        self.name.selectAll()

    def defer_connection(self):
        if not self.connection.busy:
            self._deferred = True
            self.show_knowledge_base()

    def choose_directory(self):
        selected = QFileDialog.getExistingDirectory(
            self, "选择知识库存放文件夹", self.directory.text()
        )
        if selected:
            self.directory.setText(selected)

    def target(self):
        name = self.name.text().strip()
        if not name or name in {".", ".."} or any(c in name for c in '/\\:*?"<>|'):
            raise ValueError("请填写知识库名称，名称中不能包含路径分隔符或特殊文件字符。")
        directory = self.directory.text().strip()
        if not directory:
            raise ValueError("请选择知识库存放文件夹。")
        parent = Path(directory).expanduser()
        if not parent.is_absolute():
            raise ValueError("存放文件夹需要使用完整路径。")
        return (parent / name).resolve()

    def advance(self):
        if self._creating or self.connection.busy or not self.connection.view:
            return
        if self.step < 2:
            if self.connection.validate():
                self.connection.set_step(self.step + 1)
        elif self.step == 2:
            if self.connection.state and self.connection.state.state == "passed":
                self.connection_verified(self.connection.state)
            else:
                self.connection.verify()
        elif self.step == 3:
            try:
                root = self.target()
            except ValueError as error:
                self.kb_status.setText(str(error))
                self.update_controls()
                return
            model_name = (
                "稍后配置" if self._deferred else display_model(self.connection.state.model)
            )
            self.kb_status.clear()
            self.create_status.clear()
            self.review.setText(
                f"知识库：{root.name}\n\n保存位置：{root}\n\n"
                f"模型：{model_name}\n\n"
                f"知识内容语言：{self.language.currentText()}"
            )
            self.pages.setCurrentIndex(2)
            self.update_controls()
        else:
            self.create_knowledge_base()

    def go_back(self):
        if self.step < 3:
            self.connection.set_step(self.step - 1)
        else:
            self.pages.setCurrentIndex(self.pages.currentIndex() - 1)
            self.update_controls()

    def update_controls(self):
        if not hasattr(self, "primary"):
            return
        busy = self.connection.busy or self._creating
        self.progress.setText(f"第 {self.step + 1} 步，共 {5 if self.creating else 3} 步")
        self.title.setText(STEPS[self.step])
        self.back.setEnabled(self.step > 0 and not busy)
        self.primary.setEnabled(not busy and self.connection.view is not None)
        self.connection.status.setVisible(self.pages.currentIndex() == 0)
        if self.creating:
            self.kb_status.setVisible(self.step == 3 and bool(self.kb_status.text()))
            self.create_status.setVisible(self.step == 4 and bool(self.create_status.text()))
        if self.step == 0 and self.connection.service.currentIndex() < 0:
            self.primary.setEnabled(False)
        self.later.setEnabled(not self._creating)
        self.skip.setVisible(self.creating and self.step < 3)
        self.skip.setEnabled(not busy)
        self.primary.setText(
            "正在创建…"
            if self._creating
            else "下一步"
            if self.step in {0, 1}
            else (
                "使用已验证连接继续"
                if self.connection.state and self.connection.state.state == "passed"
                else "保存并测试连接"
            )
            if self.step == 2
            else "检查并创建"
            if self.step == 3
            else "创建知识库"
        )

    def create_knowledge_base(self):
        root = self.target()
        language = self.language.currentData()
        self._creating = True
        self.pages.setEnabled(False)
        self.create_status.setText("正在创建知识库…")
        self.update_controls()
        deferred = self._deferred

        def create():
            if not deferred and read_connection_state().state != "passed":
                raise ValueError("模型连接已变更。请返回模型步骤，重新测试后创建。")
            return initialize_kb(
                root, language=language, seed_environment=False, require_empty=True
            )

        def created(_, error):
            self._creating = False
            self.pages.setEnabled(True)
            if error:
                self.create_status.setText(
                    "此位置已经有知识库。请返回上一步更换名称，或关闭引导后打开已有知识库。"
                    if isinstance(error, FileExistsError)
                    else str(error)
                    if isinstance(error, ValueError)
                    else "创建未完成。请检查文件夹写入权限和可用空间，然后重试。"
                )
                self.update_controls()
                return
            self.accept()
            self.window.open_knowledge_base(root, workspace="资料")
            self.window.statusBar().showMessage("知识库已创建。现在可以导入第一份资料。")

        self.window.io.submit(
            create,
            created,
            kb=root,
            exclusive=True,
            creating=True,
            global_settings=True,
            obsolete=lambda: self._closed,
        )

    def reject(self):
        if not self._creating:
            super().reject()

    def done(self, result):
        self._closed = True
        self.connection.close_setup()
        super().done(result)
