"""Native settings editor with explicit keep/set/clear and source labels."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openkb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from openkb.desktop.form_controls import FocusComboBox, scroll_form
from openkb.desktop.panels import ManagementPanel

_SOURCES = {
    "kb": "本库",
    "global": "全局",
    "default": "内置默认",
    "environment": "启动环境",
    "unset": "未设置",
}
_FIELDS = {
    "model": "模型",
    "language": "内容语言",
    "pdf_short_max_pages": "短 PDF 页数上限（含）",
    "pageindex_threshold": "兼容旧 PDF 阈值（≤0 强制分段）",
    "model_capacity": "模型容量（JSON，可清除以自动判断）",
    "download_remote_assets": "下载 HTML 远程图片（true/false）",
    "office_runtime_path": "私有 Office 目录（清除后使用随包目录）",
    "office_timeout_seconds": "Office 转换超时（秒，1–3600）",
    "entity_types": "实体类型（逗号分隔）",
    "openai_api_base": "API base URL",
    "api_key": "API Key",
}


class SettingField(QWidget):
    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.action = FocusComboBox()
        self.action.addItems(["不变", "设置", "清除覆盖"])
        self.text = QLineEdit()
        if key == "api_key":
            self.text.setEchoMode(QLineEdit.EchoMode.Password)
        self.text.textEdited.connect(lambda: self.action.setCurrentIndex(1))
        self.source = QLabel()
        layout.addWidget(self.action)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.source)

    def load(self, value, source):
        self.action.setCurrentIndex(0)
        self.text.clear()
        if self.key == "api_key":
            current = "已设置（输入可更换）" if value else "未设置"
        elif isinstance(value, list):
            current = ", ".join(value)
        elif self.key == "model_capacity" and value is not None:
            current = value.model_dump_json(exclude_none=True, exclude={"schema_version"})
        else:
            current = str(value) if value is not None else "未设置"
        self.text.setPlaceholderText(current)
        self.text.setToolTip(current)
        self.source.setText(_SOURCES.get(source, source))

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        text = self.text.text().strip() if self.key != "api_key" else self.text.text()
        if not text:
            raise ValueError(f"{_FIELDS[self.key]}：请输入值，或选择清除覆盖")
        if self.key in {"pageindex_threshold", "pdf_short_max_pages", "office_timeout_seconds"}:
            try:
                value = int(text)
            except ValueError:
                raise ValueError("PDF 页数须为整数") from None
            if self.key == "pdf_short_max_pages" and value < 0:
                raise ValueError("短 PDF 页数上限须为非负整数")
            return value
        if self.key == "model_capacity":
            from openkb.processing_policy import ModelCapacity

            return ModelCapacity.model_validate_json(text).model_dump(mode="json")
        if self.key == "entity_types":
            return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
        if self.key == "download_remote_assets":
            if text.lower() not in {"true", "false"}:
                raise ValueError("下载 HTML 远程图片：请输入 true 或 false")
            return text.lower() == "true"
        return text


class SettingsDialog(ManagementPanel):
    def __init__(self, io, kb: Path | None, parent=None):
        super().__init__(parent)
        self.io, self.kb = io, kb
        self._closed = False
        self._saving = False
        self._loaded = False
        self._loading = False
        self.setWindowTitle(f"知识库设置 · {kb.name}" if kb else "全局默认设置")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        self.status = QLabel("正在读取设置…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.form = QWidget()
        form_layout = QFormLayout(self.form)
        form_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fields = {}
        for key, label in _FIELDS.items():
            field = SettingField(key)
            self.fields[key] = field
            form_layout.addRow(label, field)
        content_layout.addWidget(self.form)
        hint = QLabel(
            "清除仅移除此处覆盖，之后使用下一层设置。密钥值不会读回。\n"
            "已开始的任务保留原配置；新任务开始时读取最新设置。"
        )
        hint.setWordWrap(True)
        content_layout.addWidget(hint)
        self.policy = QLabel()
        self.policy.setWordWrap(True)
        content_layout.addWidget(self.policy)
        content_layout.addStretch()
        layout.addWidget(scroll_form(content), 1)
        self.repair_button = QPushButton("检查并恢复设置…")
        self.repair_button.clicked.connect(self.repair)
        layout.addWidget(self.repair_button)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.reload()

    def reload(self):
        # Re-entering a scope updates inheritance, but never discards pending patches.
        if (
            self._closed
            or self._saving
            or self._loading
            or any(field.action.currentIndex() != 0 for field in self.fields.values())
        ):
            return
        self._loading = True
        self.form.setEnabled(False)
        self.status.setText("正在读取设置…")
        self.io.submit(
            lambda: read_settings_view(self.kb),
            self.loaded,
            kb=self.kb,
            global_settings=True,
            obsolete=lambda: self._closed,
        )

    def loaded(self, view, error):
        if self._closed:
            return
        self._saving = self._loading = False
        self.buttons.setEnabled(True)
        if error:
            self.form.setEnabled(self._loaded)
            self.status.setText(f"设置操作失败（{type(error).__name__}），原文件及恢复资料会保留。")
            return
        self._loaded = True
        self.form.setEnabled(True)
        self.status.setText(str(self.kb) if self.kb else "全局默认：知识库与启动环境可覆盖这些值。")
        limit = view.values.pdf_limit
        capacity = view.capacity
        classification = (
            "全部分段" if limit.legacy_force_index else f"≤{limit.short_max_pages} 页为短文"
        )
        boundary = capacity.get("input_limit") if not capacity.get("unknown_reason") else "未知"
        self.policy.setText(
            f"当前 PDF：{classification}"
            f"；来源：{_SOURCES[limit.source]} / {limit.key}。\n"
            "同层新上限优先于兼容旧阈值；清除新上限后，已有旧阈值仍会生效。\n"
            f"模型输入上限：{boundary}"
            f"；来源：{capacity.get('source')}。"
            "容量未知会尝试全文；已知不足的短文使用分段编译。\n"
            "容量 JSON 可指定 max_input_tokens，或 context_window_tokens 与 output_reserve_tokens；"
            "自定义端点还需 tokenizer_model。"
        )
        for key, field in self.fields.items():
            value = view.values.has_api_key if key == "api_key" else getattr(view.values, key)
            field.load(value, view.sources[key])

    def save(self):
        if not self.form.isEnabled():
            return
        try:
            changes = {
                key: field.value()
                for key, field in self.fields.items()
                if field.action.currentIndex() != 0
            }
            credentials = {
                key: changes.pop(key) for key in ("api_key", "openai_api_base") if key in changes
            }
            if not changes and not credentials:
                return
            request = (
                KbConfigPatchRequest(kb=str(self.kb), config=changes, **credentials)
                if self.kb
                else GlobalConfigPatchRequest(config=changes, **credentials)
            )
        except ValueError as exc:
            QMessageBox.warning(self, "检查设置", str(exc))
            return
        self._saving = True
        self.form.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setText("正在等待并保存设置…")

        def operation():
            if isinstance(request, KbConfigPatchRequest):
                assert self.kb is not None
                apply_kb_config_patch(self.kb, request)
            else:
                apply_global_config_patch(request)
            return read_settings_view(self.kb)

        self.io.submit(operation, self.loaded, kb=self.kb, exclusive=True, global_settings=True)

    def repair(self):
        from openkb.application.repair import repair_global_settings, repair_knowledge_base

        if self._saving:
            return
        self._saving = True
        self.form.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setText("正在检查恢复资料及设置…")

        def operation():
            return repair_knowledge_base(self.kb) if self.kb else repair_global_settings()

        def repaired(result, error):
            self._saving = False
            self.buttons.setEnabled(True)
            if error or not result.repaired:
                self.status.setText(
                    f"恢复未完成（{type(error).__name__}），资料保留。"
                    if error
                    else "\n".join(result.issues)
                )
                return
            self.io.submit(
                lambda: read_settings_view(self.kb),
                self.loaded,
                kb=self.kb,
                global_settings=True,
                obsolete=lambda: self._closed,
            )

        self.io.submit(
            operation, repaired, kb=self.kb, exclusive=True, global_settings=True, repair=True
        )

    def reject(self):
        if not self._saving:
            self._closed = True
            super().reject()

    def closeEvent(self, event):
        if self._saving:
            event.ignore()
        else:
            self._closed = True
            super().closeEvent(event)
