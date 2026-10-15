"""Three focused connection steps, independent of the advanced settings editor."""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.model_catalog import ModelCatalogRequest, read_model_catalog
from ultrakb.application.model_connections import read_connection_state, verify_model_connection
from ultrakb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from ultrakb.desktop.form_controls import FocusComboBox


def paragraph(text, *, muted=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if muted:
        label.setObjectName("muted")
    return label


def display_model(model):
    for prefix in ("openai/", "deepseek/", "ollama_chat/"):
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


class ConnectionSetup(QWidget):
    changed = Signal()
    loaded = Signal(object)
    verified = Signal(object)

    def __init__(self, io, kb=None, parent=None):
        super().__init__(parent)
        self.io, self.kb = io, kb
        self.closed = False
        self.busy = True
        self.view = None
        self.state = None
        self._saved_address = ""
        self._stop = threading.Event()
        self._generation = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self._provider_page()
        self._credentials_page()
        self._model_page()
        self.status = paragraph("正在读取已有连接…")
        self.status.setAccessibleName("连接设置状态")
        self.status.setObjectName("setupStatus")
        layout.addWidget(self.status)
        self.cancel_test = QPushButton("取消测试")
        self.cancel_test.setMinimumHeight(44)
        self.cancel_test.clicked.connect(self.cancel)
        self.cancel_test.hide()
        layout.addWidget(self.cancel_test)
        self.pages.setEnabled(False)
        self.io.submit(
            lambda: (read_settings_view(kb), read_connection_state(kb)),
            self._loaded,
            kb=kb,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.closed,
        )

    def _page(self, text):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(paragraph(text))
        self.pages.addWidget(page)
        return layout

    def _provider_page(self):
        layout = self._page("选择将用于整理资料和回答问题的模型服务。")
        self.service = FocusComboBox()
        self.service.setMinimumHeight(44)
        self.service.setAccessibleName("服务提供商")
        self.service.addItems([t.name for t in CONNECTION_TEMPLATES])
        self.service.setPlaceholderText("选择服务提供商")
        self.service.setCurrentIndex(-1)
        self.service.currentIndexChanged.connect(self.choose_provider)
        layout.addWidget(self.service)
        self.help = paragraph("自定义连接适用于提供 OpenAI 兼容 API 的服务或代理。", muted=True)
        layout.addWidget(self.help)
        layout.addStretch()

    def _credentials_page(self):
        layout = self._page("填写所选服务的连接信息。API Key 默认隐藏，已有密钥不会回显。")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setSpacing(8)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://your-service.example/v1")
        self.base_url.setAccessibleName("API 服务地址")
        self.base_url.setMinimumHeight(44)
        form.addRow("API 服务地址", self.base_url)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setAccessibleName("API Key")
        self.api_key.setPlaceholderText("输入此服务的 API Key")
        self.api_key.setMinimumHeight(44)
        form.addRow("API Key", self.api_key)
        layout.addLayout(form)
        self.show_key = QCheckBox("显示 API Key")
        self.show_key.setMinimumHeight(44)
        self.show_key.toggled.connect(
            lambda show: self.api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(self.show_key)
        self.credential_hint = paragraph("", muted=True)
        layout.addWidget(self.credential_hint)
        self.base_url.textChanged.connect(self.edited)
        self.api_key.textChanged.connect(self.edited)
        layout.addStretch()

    def _model_page(self):
        layout = self._page("选择模型，也可以填写服务商提供的模型名称。")
        self.model = FocusComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(FocusComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumHeight(44)
        self.model.setAccessibleName("模型名称")
        self.model.currentTextChanged.connect(self.edited)
        layout.addWidget(self.model)
        self.fetch_models = QPushButton("获取模型列表")
        self.fetch_models.setMinimumHeight(44)
        self.fetch_models.clicked.connect(self.load_models)
        layout.addWidget(self.fetch_models)
        self.summary = paragraph("")
        layout.addWidget(self.summary)
        layout.addWidget(
            paragraph(
                "连接测试会发送少量合成内容，可能产生少量用量；测试不包含知识库资料。", muted=True
            )
        )
        layout.addStretch()

    @property
    def step(self):
        return self.pages.currentIndex()

    def set_step(self, step):
        self.pages.setCurrentIndex(step)
        (self.service, self.base_url, self.model)[step].setFocus()
        if step == 2:
            self.summary.setText(
                f"服务地址：{self.base_url.text().strip()}\n保存范围：{self.scope}"
            )
        self.changed.emit()

    @property
    def scope(self):
        return f"仅知识库「{self.kb.name}」" if self.kb else "全局默认，供新知识库继承"

    def _loaded(self, result, error):
        self.busy = False
        if error:
            self.status.setText("无法读取连接设置。请关闭引导，在设置中检查并恢复配置后重试。")
            self.changed.emit()
            return
        self.view, self.state = result
        values = self.view.values
        # Infer the service from the effective endpoint, rather than the last
        # example clicked in a settings panel.
        index = next(
            (
                i
                for i, template in enumerate(CONNECTION_TEMPLATES)
                if i and values.openai_api_base == template.base_url
            ),
            0 if values.has_api_key or values.openai_api_base else -1,
        )
        if not values.openai_api_base and values.has_api_key:
            provider = values.model.split("/", 1)[0]
            index = {"openai": 1, "deepseek": 2, "ollama_chat": 3}.get(provider, 0)
            if values.model.startswith(("gpt-", "o1", "o3", "o4")):
                index = 1
        self.service.setCurrentIndex(index)
        self._saved_address = (
            values.openai_api_base
            or (CONNECTION_TEMPLATES[index].base_url if index > 0 else "")
            or ""
        )
        self.base_url.setText(self._saved_address)
        self.model.setEditText(display_model(values.model))
        if values.has_api_key:
            self.api_key.setPlaceholderText("已保存；留空继续使用，输入可更换")
        self.credential_hint.setText(
            "启动环境提供了密钥或地址；全局设置不能覆盖启动环境。本库设置可单独覆盖。"
            if not self.kb and "environment" in self.view.sources.values()
            else f"连接将保存到{self.scope}。其他参数可在创建后进入设置调整。"
        )
        self.pages.setEnabled(True)
        # Field initialization emits edits; restore the actual captured state last.
        self.state = result[1]
        self.status.setText(
            self.state.message
            if self.state.state == "passed"
            else "先选择服务提供商，再填写连接信息。已有设置会自动带入。"
        )
        if self.state.state == "passed":
            self.set_step(2)
        self.changed.emit()
        self.loaded.emit(self.state)

    def choose_provider(self, index):
        if index < 0:
            return
        template = CONNECTION_TEMPLATES[index]
        self.api_key.clear()
        self.show_key.setChecked(False)
        self.help.setText(template.explanation)
        if template.model is not None:
            self.base_url.setText(template.base_url or "")
            self.model.clear()
            self.model.addItem(display_model(template.model))
        else:
            self.base_url.clear()
            self.model.clear()
        self.edited()

    def edited(self, *_):
        self.state = None
        self._generation += 1
        if self.view:
            self.status.setText("连接有修改，将在保存后测试当前生效连接。")
        self.changed.emit()

    def validate(self, *, model_required=True):
        if self.service.currentIndex() < 0:
            self.status.setText("请先选择一个服务提供商。")
            self.service.setFocus()
            return False
        if self.step == 0:
            return True
        address = self.base_url.text().strip()
        try:
            parsed = urlsplit(address)
        except ValueError:
            self.status.setText("服务地址格式不正确，请检查后重试。")
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            self.status.setText("请填写完整的 API 服务地址，例如 https://your-service.example/v1。")
            self.base_url.setFocus()
            return False
        local = self.service.currentIndex() == 3
        reusable = self.view and self.view.values.has_api_key and address == self._saved_address
        if not local and not self.api_key.text().strip() and not reusable:
            self.status.setText("请填写此服务的 API Key；切换服务地址后需要重新填写密钥。")
            self.api_key.setFocus()
            return False
        if self.step == 2 and model_required and not self.model.currentText().strip():
            self.status.setText("请选择或填写模型名称。")
            self.model.setFocus()
            return False
        return True

    def verify(self):
        if self.busy or not self.view or not self.validate():
            return
        self.busy = True
        self.pages.setEnabled(False)
        self._stop = threading.Event()
        stop = self._stop
        generation = self._generation
        model, address = self.model.currentText().strip(), self.base_url.text().strip()
        values = self.view.values
        if model == display_model(values.model):
            model = values.model
        elif CONNECTION_TEMPLATES[
            self.service.currentIndex()
        ].model is not None and model == display_model(
            CONNECTION_TEMPLATES[self.service.currentIndex()].model
        ):
            model = CONNECTION_TEMPLATES[self.service.currentIndex()].model
        else:
            prefix = {0: "openai/", 1: "openai/", 2: "deepseek/", 3: "ollama_chat/"}[
                self.service.currentIndex()
            ]
            if not model.startswith(prefix):
                model = prefix + model
        changes = {}
        if model != values.model:
            changes["model"] = model
        template = CONNECTION_TEMPLATES[self.service.currentIndex()]
        if (
            model != values.model
            or address != self._saved_address
            or (values.model_capacity is None and model == template.model)
        ):
            changes["model_capacity"] = (
                template_capacity(template) if model == template.model else None
            )
        credentials = {}
        if address != self._saved_address:
            credentials["openai_api_base"] = address
        if self.api_key.text().strip():
            credentials["api_key"] = self.api_key.text().strip()
        request = (
            KbConfigPatchRequest(kb=str(self.kb), config=changes or None, **credentials)
            if self.kb
            else GlobalConfigPatchRequest(config=changes or None, **credentials)
        )
        self.status.setText("正在保存连接…")
        self.changed.emit()

        def save():
            if self.kb:
                apply_kb_config_patch(self.kb, request)
            else:
                apply_global_config_patch(request)
            return read_settings_view(self.kb)

        def saved(view, error):
            if error:
                self._finished(None, error, generation)
                return
            self.view = view
            self._saved_address = address
            self.api_key.clear()
            self.model.setEditText(display_model(view.values.model))
            self.show_key.setChecked(False)
            self.api_key.setPlaceholderText("已保存；留空继续使用，输入可更换")
            # Clearing the secret is presentation, not a connection edit.
            self._generation = generation
            self.status.setText("正在测试文本响应与工具调用…最多等待 20 秒。")
            self.cancel_test.show()
            self.cancel_test.setEnabled(True)
            self.io.submit(
                lambda: verify_model_connection(
                    self.kb,
                    cancelled=lambda: stop.is_set() or self.closed or self.io._stop.is_set(),
                ),
                lambda state, error: self._finished(state, error, generation),
                obsolete=lambda: self.closed,
            )

        self.io.submit(
            save,
            saved,
            kb=self.kb,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.closed,
        )

    def load_models(self):
        if self.busy or not self.validate(model_required=False):
            return
        request = ModelCatalogRequest(
            base_url=self.base_url.text().strip(),
            api_key=self.api_key.text().strip() or None,
            local=self.service.currentIndex() == 3,
        )
        self.busy = True
        self.pages.setEnabled(False)
        self.status.setText("正在获取模型列表…最多等待 10 秒，也可以稍后手动填写。")
        self.changed.emit()

        def loaded(result, error):
            self.busy = False
            self.pages.setEnabled(True)
            if error:
                self.status.setText("无法获取模型列表，可以手动填写模型名称后测试连接。")
            else:
                current = self.model.currentText()
                self.model.blockSignals(True)
                self.model.clear()
                self.model.addItems(result.models)
                self.model.setEditText(current)
                self.model.blockSignals(False)
                self.status.setText(result.message)
            self.changed.emit()

        self.io.submit(
            lambda: read_model_catalog(request, self.kb),
            loaded,
            obsolete=lambda: self.closed,
        )

    def _finished(self, state, error, generation):
        self.busy = False
        self.pages.setEnabled(True)
        self.cancel_test.hide()
        self.state = state
        if self._stop.is_set():
            self.status.setText("测试已取消。已保存的连接保留，可以重试或稍后继续。")
        elif error:
            self.status.setText("连接操作未完成。请检查地址、密钥和网络，然后重试。")
        elif generation != self._generation:
            self.state = None
            self.status.setText("连接已变化，请重新测试当前连接。")
        else:
            self.status.setText(state.message)
            if state.state == "passed":
                self.verified.emit(state)
        self.changed.emit()

    def cancel(self):
        self._stop.set()
        self.cancel_test.setEnabled(False)
        self.status.setText("正在取消测试…已保存的连接会保留。")

    def close_setup(self):
        self.closed = True
        self._stop.set()
