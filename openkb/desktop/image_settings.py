"""Independent image model settings, explicit enablement and sample-image verification."""

import asyncio

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QPushButton

from openkb.desktop.processing_settings import SettingsSection
from openkb.vision.config import VisionSettings


class ImageField(SettingsSection):
    def __init__(self, io, kb):
        super().__init__(
            "图片理解默认关闭。开启后，仅按需将目标图片发送到所选服务。"
            "知识编译可以继续使用纯文本模型；OCR 设置独立生效。"
        )
        from openkb.desktop.settings import SettingField

        self.io, self.kb = io, kb
        self._saved = {}
        self.enabled = QCheckBox("启用图片理解")
        self.enabled.toggled.connect(self.changed)
        self.body.addWidget(self.enabled)
        self.connection = QComboBox()
        self.connection.addItem("独立图片模型连接", "independent")
        self.connection.addItem("明确复用主模型完整连接", "reuse_main")
        self.provider = QComboBox()
        for value in ("openai", "openai-compatible", "anthropic", "ollama"):
            self.provider.addItem(value, value)
        self.model, self.endpoint = QLineEdit(), QLineEdit()
        self.api_key = SettingField("image_api_key")
        self.authentication = QComboBox()
        self.authentication.addItem("使用此图片连接的 API Key", "api_key")
        self.authentication.addItem("服务无需鉴权", "none")
        self.supports_images = QCheckBox("此端点与模型支持图片输入（保存后测试确认）")
        form = QFormLayout()
        for label, entry in (
            ("连接方式", self.connection),
            ("提供商 / 协议", self.provider),
            ("图片模型", self.model),
            ("API 地址", self.endpoint),
            ("鉴权方式", self.authentication),
            ("图片 API Key", self.api_key),
        ):
            entry.setAccessibleName(label)
            form.addRow(label, entry)
        self.body.addLayout(form)
        self.body.addWidget(self.supports_images)
        for combo in (self.connection, self.provider, self.authentication):
            combo.activated.connect(self.changed)
        for entry in (self.model, self.endpoint):
            entry.textEdited.connect(self.changed)
        self.supports_images.toggled.connect(self.changed)
        self.test = QPushButton("测试已保存连接（发送内置样本图片）")
        self.test.clicked.connect(self.verify)
        self.body.addWidget(self.test)
        self.status = QLabel("保存连接不会开启图片理解；测试不会发送知识库资料。")
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.body.addStretch()

    def load(self, value, source):
        settings = value or VisionSettings()
        self._saved = settings.model_dump()
        self.enabled.setChecked(settings.enabled)
        self.supports_images.setChecked(settings.supports_images)
        for name in ("provider", "connection", "authentication"):
            combo = getattr(self, name)
            combo.setCurrentIndex(combo.findData(getattr(settings, name)))
        self.model.setText(settings.model)
        self.endpoint.setText(settings.endpoint or "")
        self.loaded(source)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        try:
            return VisionSettings.model_validate(
                {
                    **self._saved,
                    "enabled": self.enabled.isChecked(),
                    "supports_images": self.supports_images.isChecked(),
                    "provider": self.provider.currentData(),
                    "connection": self.connection.currentData(),
                    "model": self.model.text().strip(),
                    "endpoint": self.endpoint.text().strip() or None,
                    "authentication": self.authentication.currentData(),
                }
            ).model_dump()
        except ValueError:
            raise ValueError("请检查图片模型和不含密钥的 API 地址。") from None

    def verify(self):
        if self.action.currentIndex() or self.api_key.action.currentIndex():
            self.status.setText("请先保存连接，再测试已保存的提供商与模型。")
            return
        from openkb.application.image_understanding import test_image_connection

        self.test.setEnabled(False)
        self.status.setText("正在向已保存的图片模型发送内置样本…")

        def finished(result, error):
            self.test.setEnabled(True)
            self.status.setText(
                "图片连接测试失败"
                if error
                else ("图片输入已验证" if result["status"] == "ready" else result["status"])
            )

        self.io.submit(
            lambda: asyncio.run(test_image_connection(self.kb)),
            finished,
            kb=self.kb,
            global_settings=True,
        )
