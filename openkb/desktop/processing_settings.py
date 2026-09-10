"""Native controls for finite model budgets and independently saved OCR profiles."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openkb.ocr.config import ParsingSettings
from openkb.processing import ProcessingIncomplete, RequestLimits


class ValueForm(QWidget):
    def __init__(self, definitions, parent=None):
        super().__init__(parent)
        self.inputs = {}
        self.definitions = definitions
        form = QFormLayout(self)
        for key, label, kind in definitions:
            entry = QLineEdit()
            entry.setAccessibleName(label)
            self.inputs[key] = entry
            form.addRow(label, entry)

    def load(self, values):
        for key, entry in self.inputs.items():
            entry.setText(str(values.get(key, "")))

    def value(self):
        values = {}
        for key, label, kind in self.definitions:
            text = self.inputs[key].text().strip()
            try:
                values[key] = kind(text)
            except (ValueError, TypeError):
                raise ValueError(f"{label}：请输入有效值") from None
            if not text:
                raise ValueError(f"{label}：请输入值")
        return values


class SettingsSection(QWidget):
    def __init__(self, explanation):
        super().__init__()
        self.body = QVBoxLayout(self)
        self.action = QComboBox()
        self.action.addItems(["不变", "设置", "清除覆盖"])
        self.source = QLabel()
        hint = QLabel(explanation)
        hint.setWordWrap(True)
        for widget in (self.action, self.source, hint):
            self.body.addWidget(widget)

    def changed(self, *_):
        self.action.setCurrentIndex(1)

    def loaded(self, source):
        self.source.setText(
            "当前配置来自："
            + {"kb": "本库", "global": "全局", "default": "默认"}.get(source, source)
        )
        self.action.setCurrentIndex(0)


class ProcessingField(SettingsSection):
    def __init__(self):
        super().__init__(
            "知识编译默认从 256K 上下文、128K 输出开始；截断后逐档增加到 1M／384K，"
            "仍截断则拆小批次重试。单文档累计 token 默认不限。"
            "可按模型能力调整；清除覆盖后恢复继承。"
        )
        self.values = ValueForm(
            [
                ("context_tokens", "初始上下文（token）", int),
                ("output_tokens", "初始输出（token）", int),
                ("max_context_tokens", "模型最大上下文（token）", int),
                ("max_output_tokens", "模型最大输出（token）", int),
                ("request_timeout", "单次请求时限（秒）", float),
                ("stage_timeout", "单个阶段时限（秒）", float),
                ("document_timeout", "整份资料时限（秒）", float),
                ("cleanup_timeout", "任务收尾时限（秒）", float),
                ("max_attempts", "单次操作最多尝试数", int),
                ("max_requests", "整份资料最多请求数", int),
                ("max_tokens", "累计 token 上限（0 表示不限）", lambda text: int(text) or None),
                ("concurrency", "同时进行的模型请求上限", int),
            ]
        )
        self.body.addWidget(self.values)
        self.body.addStretch()
        for entry in self.values.inputs.values():
            entry.textEdited.connect(self.changed)

    def load(self, value, source):
        values = dict(value or {})
        for key in ("context_tokens", "output_tokens"):
            values.setdefault("max_" + key, values.get(key, ""))
        if values.get("max_tokens") is None:
            values["max_tokens"] = 0
        self.values.load(values)
        self.loaded(source)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        result = self.values.value()
        try:
            RequestLimits.from_config({"processing": result})
        except ProcessingIncomplete:
            raise ValueError(
                "初始值不得超过模型最大值，输出须小于上下文；累计 token 可填 0，其余须为正数。"
            ) from None
        return result


class NavigationField(SettingsSection):
    def __init__(self):
        super().__init__(
            "本地 PageIndex 可增强原文导航，额度独立于知识编译；关闭时仍可按原文位置查阅。"
        )
        self.enabled = QCheckBox("启用本地导航增强")
        self.enabled.toggled.connect(self.changed)
        self.budget = ProcessingField()
        self.budget.action.hide()
        self.budget.source.hide()
        self.body.addWidget(self.enabled)
        self.body.addWidget(self.budget)
        for entry in self.budget.values.inputs.values():
            entry.textEdited.connect(self.changed)

    def load(self, value, source):
        self.enabled.setChecked(value.enabled)
        self.budget.load(value.processing, source)
        self.loaded(source)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        return {
            "enabled": self.enabled.isChecked(),
            "processing": self.budget.value() if self.enabled.isChecked() else None,
        }


class OcrField(SettingsSection):
    def __init__(self, io=None, kb=None):
        self.io, self.kb = io, kb
        super().__init__(
            "可靠文字层直接读取；需要识别的页使用选定后端。本地与云配置分别保存。"
            "云端 API Key 可直接输入，保存后仅显示是否已设置。"
        )
        from openkb.desktop.settings import SettingField

        self.cloud_key = SettingField("ocr_api_key")
        self.cloud_key.text.setAccessibleName("OCR 云端 API Key")
        self.policy = QComboBox()
        self.policy.addItem("自动识别需要 OCR 的内容", "auto")
        self.policy.addItem("关闭 OCR，保留原文和图片", "off")
        self.policy.activated.connect(self.changed)
        self.body.addWidget(self.policy)
        self.backend = QComboBox()
        self.backend.addItem("系统 OCR（默认）", "system")
        self.backend.addItem("本地飞桨模型", "local")
        self.backend.addItem("PaddleOCR 云 jobs 服务", "cloud")
        self.backend.setAccessibleName("需要 OCR 时使用")
        self.backend.activated.connect(self.changed)
        self.body.addWidget(self.backend)
        self.device = QComboBox()
        for label, value in (
            ("自动：优先可用 GPU", "auto"),
            ("只用 CPU", "cpu"),
            ("指定 GPU，不回退", "gpu"),
        ):
            self.device.addItem(label, value)
        self.device.activated.connect(self.changed)
        self.body.addWidget(self.device)
        self.gpu_device = QLineEdit()
        self.gpu_device.setPlaceholderText("可选设备编号，例如 GPU.0（Intel）或 gpu:0（NVIDIA）")
        self.gpu_device.textEdited.connect(self.changed)
        self.body.addWidget(self.gpu_device)
        from openkb.desktop.ocr_installation import OcrInstallationPanel, fill_installations

        self.installation = QComboBox()
        fill_installations(self.installation)
        self.installation.activated.connect(self.changed)
        self.body.addWidget(self.installation)
        self.installer = OcrInstallationPanel(io) if io else None
        if self.installer:
            self.body.addWidget(self.installer)

            def installed(identity):
                fill_installations(self.installation, identity)
                self.changed()

            self.installer.installed.connect(installed)
        self.execution = QComboBox()
        self.execution.addItem("使用已安装运行环境", "runtime")
        self.execution.addItem("连接已部署的飞桨服务", "service")
        self.execution.activated.connect(self.changed)
        self.body.addWidget(self.execution)
        self.service_endpoint = QLineEdit()
        self.service_endpoint.setPlaceholderText("http://127.0.0.1:8080（其他主机属于远端处理）")
        self.service_endpoint.textEdited.connect(self.changed)
        self.service_protocol = QComboBox()
        self.service_protocol.addItem("完整解析服务 /layout-parsing", "pipeline")
        self.service_protocol.addItem("仅 VLM（结果需要版面复核）", "vlm")
        self.service_protocol.activated.connect(self.changed)
        self.body.addWidget(self.service_endpoint)
        self.body.addWidget(self.service_protocol)
        self.check_button = QPushButton("检查已保存服务 / 所选运行环境（内置样本）")
        self.check_button.clicked.connect(self.check_capability)
        self.check_status = QLabel()
        self.check_status.setWordWrap(True)
        self.body.addWidget(self.check_button)
        self.body.addWidget(self.check_status)
        self.device.currentIndexChanged.connect(self.visibility)
        self.execution.currentIndexChanged.connect(self.visibility)
        self.backend.currentIndexChanged.connect(self.visibility)
        self.policy.currentIndexChanged.connect(self.visibility)
        self.advanced = QCheckBox("高级：已有运行环境、资源额度与云连接")
        self.body.addWidget(self.advanced)
        self.tabs = QTabWidget()
        self.body.addWidget(self.tabs)
        self.advanced.toggled.connect(self.tabs.setVisible)
        self.tabs.hide()
        self.enabled, self.forms = {}, {}
        self.cloud_options = {}
        self._profiles = {}
        local = [
            ("interpreter", "可选 OCR Python 解释器（绝对路径）", str),
            ("assets", "离线模型目录（绝对路径）", str),
            ("assets_sha256", "模型 manifest.json SHA-256", str),
        ]
        cloud = [
            ("endpoint", "云 jobs 端点", str),
            ("model", "云模型名称", str),
        ]
        for name, title, identity in (("local", "本地配置", local), ("cloud", "云配置", cloud)):
            panel = QWidget()
            layout = QVBoxLayout(panel)
            self.enabled[name] = QCheckBox("保存" + title)
            self.enabled[name].toggled.connect(self.changed)
            layout.addWidget(self.enabled[name])
            fields = {"identity": ValueForm(identity)}
            limits = [
                ("seconds", "本轮 OCR 总时限（秒）", float),
                ("max_pages", "本轮最多处理页数", int),
            ]
            if name == "local":
                limits += [
                    ("cleanup_seconds", "识别进程收尾时限（秒）", float),
                    ("memory_bytes", "识别进程内存上限（字节）", int),
                    ("output_bytes", "单页输出磁盘上限（字节）", int),
                    ("max_regions", "本轮最多识别区域数", int),
                    ("max_tokens", "本轮识别输出预留上限（token）", int),
                ]
                fields["parameters"] = ValueForm(
                    [
                        ("max_new_tokens", "每个区域输出上限（token）", int),
                        ("min_pixels", "区域识别最小像素数", int),
                        ("max_pixels", "区域识别最大像素数", int),
                        ("render_dpi", "PDF 渲染分辨率（DPI）", int),
                        ("threads", "CPU 线程数", int),
                    ]
                )
            else:
                limits += [
                    ("request_seconds", "云请求时限（秒）", float),
                    ("poll_seconds", "云任务查询间隔（秒）", float),
                    ("max_requests", "本轮云请求上限", int),
                    ("max_page_bytes", "单页上传上限（字节，最多 48000000）", int),
                    ("max_download_bytes", "本轮下载上限（字节）", int),
                ]
            fields["limits"] = ValueForm(limits)
            for group, form in fields.items():
                layout.addWidget(form)
                for entry in form.inputs.values():
                    entry.textEdited.connect(self.changed)
                if name == "cloud" and group == "identity":
                    layout.addWidget(QLabel("云端 API Key"))
                    layout.addWidget(self.cloud_key)
            if name == "cloud":
                for key, label in (
                    ("use_doc_orientation_classify", "云端自动判断页面方向"),
                    ("use_doc_unwarping", "云端校正页面弯曲"),
                ):
                    entry = QCheckBox(label)
                    entry.toggled.connect(self.changed)
                    self.cloud_options[key] = entry
                    layout.addWidget(entry)
            self.forms[name] = fields
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            self.tabs.addTab(scroll, title)

    def load(self, value, source):
        settings = value or ParsingSettings()
        self._profiles = settings.ocr.model_dump()
        self.policy.setCurrentIndex(self.policy.findData(settings.ocr.policy))
        self.backend.setCurrentIndex(self.backend.findData(settings.ocr.backend))
        self.device.setCurrentIndex(self.device.findData(settings.ocr.device))
        self.gpu_device.setText(settings.ocr.gpu_device or "")
        from openkb.desktop.ocr_installation import fill_installations

        fill_installations(self.installation, settings.ocr.installation)
        self.execution.setCurrentIndex(self.execution.findData(settings.ocr.execution))
        service = settings.ocr.service
        self.service_endpoint.setText(service.endpoint if service else "")
        self.service_protocol.setCurrentIndex(
            self.service_protocol.findData(service.protocol if service else "pipeline")
        )
        self.visibility()
        for name, fields in self.forms.items():
            saved = self._profiles.get(name) or {}
            self.enabled[name].setChecked(bool(saved))
            for group, form in fields.items():
                form.load(saved if group == "identity" else saved.get(group, {}))
            if name == "cloud":
                for key, entry in self.cloud_options.items():
                    entry.setChecked(saved.get("options", {}).get(key, False))
        self.loaded(source)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        result = {
            "backend": self.backend.currentData(),
            "policy": self.policy.currentData(),
            "device": self.device.currentData(),
            "installation": self.installation.currentData(),
            "gpu_device": self.gpu_device.text().strip() or None,
        }
        result["execution"] = self.execution.currentData()
        result["service"] = self._profiles.get("service")
        if self.service_endpoint.text().strip():
            result["service"] = {
                **(result["service"] or {}),
                "endpoint": self.service_endpoint.text().strip(),
                "protocol": self.service_protocol.currentData(),
            }
        for name, fields in self.forms.items():
            profile = dict(self._profiles.get(name) or {})
            if self.enabled[name].isChecked() and not (name == "local" and result["installation"]):
                profile.update(fields["identity"].value())
                for group in fields.keys() - {"identity"}:
                    profile[group] = fields[group].value()
                if name == "cloud":
                    profile["options"] = {
                        **profile.get("options", {}),
                        **{key: entry.isChecked() for key, entry in self.cloud_options.items()},
                    }
                result[name] = profile
            else:
                result[name] = None
        try:
            return ParsingSettings.model_validate({"ocr": result}).model_dump()
        except ValueError:
            raise ValueError("请检查 OCR 路径、模型、像素范围与有限正数额度。") from None

    def visibility(self, *_):
        local = self.backend.currentData() == "local" and self.policy.currentData() != "off"
        service = local and self.execution.currentData() == "service"
        self.check_button.setVisible(local and self.io is not None)
        self.check_status.setVisible(local)
        self.execution.setVisible(local)
        self.service_endpoint.setVisible(service)
        self.service_protocol.setVisible(service)
        local = local and not service
        self.device.setVisible(local)
        self.gpu_device.setVisible(local and self.device.currentData() != "cpu")
        self.installation.setVisible(local)
        if self.installer:
            self.installer.setVisible(local)
        self.backend.setEnabled(self.policy.currentData() != "off")

    def check_capability(self):
        from functools import partial

        from openkb.application.ocr_capability import check_ocr_capability, check_ocr_service

        self.installer.stop.clear()
        cancelled = self.installer.stop.is_set
        if self.execution.currentData() == "service":
            if self.action.currentIndex():
                self.check_status.setText("请先保存服务连接，再检查。")
                return
            operation = partial(check_ocr_service, self.kb, cancelled=cancelled)
        else:
            identity, device = self.installation.currentData(), self.device.currentData()
            if not identity:
                self.check_status.setText("请先安装并选择运行环境。")
                return
            operation = partial(
                check_ocr_capability,
                identity,
                device,
                gpu_device=self.gpu_device.text().strip() or None,
                cancelled=cancelled,
            )
        self.check_button.setEnabled(False)
        self.check_status.setText("正在检查完整管线与内置样本…")

        def finished(result, error):
            self.check_button.setEnabled(True)
            if error:
                self.check_status.setText("检查未完成；请核对安装或连接设置。")
            else:
                import json

                self.check_status.setText(json.dumps(result, ensure_ascii=False))

        self.io.submit(operation, finished, kb=self.kb, obsolete=cancelled)
