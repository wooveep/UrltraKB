"""Native controls for finite model budgets and independently saved OCR profiles."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
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
            "知识编译自动使用默认额度，无需填写。需要调整时选择“设置”；清除覆盖后恢复继承。"
            "请求上限应在模型支持范围内，输出上限必须小于上下文上限。"
        )
        self.values = ValueForm(
            [
                ("context_tokens", "单次请求上下文上限（token）", int),
                ("output_tokens", "单次输出上限（token）", int),
                ("request_timeout", "单次请求时限（秒）", float),
                ("stage_timeout", "单个阶段时限（秒）", float),
                ("document_timeout", "整份资料时限（秒）", float),
                ("cleanup_timeout", "任务收尾时限（秒）", float),
                ("max_attempts", "单次操作最多尝试数", int),
                ("max_requests", "整份资料最多请求数", int),
                ("max_tokens", "整份资料累计 token 上限", int),
                ("concurrency", "同时进行的模型请求上限", int),
            ]
        )
        self.body.addWidget(self.values)
        self.body.addStretch()
        for entry in self.values.inputs.values():
            entry.textEdited.connect(self.changed)

    def load(self, value, source):
        self.values.load(value or {})
        self.loaded(source)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        result = self.values.value()
        try:
            RequestLimits.from_config({"processing": result})
        except ProcessingIncomplete:
            raise ValueError("处理额度必须为有限正数，输出上限必须小于模型上下文容量。") from None
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
    def __init__(self):
        super().__init__(
            "可靠文字层直接读取；需要识别的页使用选定后端。本地与云配置分别保存。"
            "云端 API Key 可直接输入，保存后仅显示是否已设置。"
        )
        from openkb.desktop.settings import SettingField

        self.cloud_key = SettingField("ocr_api_key")
        self.cloud_key.text.setAccessibleName("OCR 云端 API Key")
        self.backend = QComboBox()
        self.backend.addItem("本地 PaddleOCR-VL-1.6（CPU）", "local")
        self.backend.addItem("PaddleOCR 云 jobs 服务", "cloud")
        self.backend.setAccessibleName("需要 OCR 时使用")
        self.backend.activated.connect(self.changed)
        self.body.addWidget(self.backend)
        self.tabs = QTabWidget()
        self.body.addWidget(self.tabs)
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
        self.backend.setCurrentIndex(0 if settings.ocr.backend == "local" else 1)
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
        result = {"backend": self.backend.currentData()}
        for name, fields in self.forms.items():
            profile = dict(self._profiles.get(name) or {})
            if self.enabled[name].isChecked():
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
