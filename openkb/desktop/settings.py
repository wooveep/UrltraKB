"""Direct settings editing with scoped patches, progressive detail and a fixed save bar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openkb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from openkb.desktop.form_controls import scroll_form
from openkb.desktop.panels import ManagementPanel
from openkb.desktop.setting_actions import SettingActions

_FIELDS = {
    "model": "模型名称",
    "language": "内容语言",
    "pageindex_threshold": "长文档索引阈值（页）",
    "entity_types": "实体类型（逗号分隔）",
    "openai_api_base": "API 地址",
    "api_key": "API Key",
    "ocr_api_key": "OCR 云端 API Key",
    "image_api_key": "图片理解 API Key",
}
_SECRET_FIELDS = {
    "api_key": "has_api_key",
    "ocr_api_key": "has_ocr_api_key",
    "image_api_key": "has_image_api_key",
}


class SettingField(QWidget):
    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.controls = SettingActions(_FIELDS[key])
        self.action, self.source = self.controls.action, self.controls.source
        self.text = QLineEdit()
        self.text.setAccessibleName(_FIELDS[key])
        self.text.setMinimumWidth(120)
        self._original = (None, "unset")
        self._initial_text = ""
        if key in _SECRET_FIELDS:
            self.text.setEchoMode(QLineEdit.EchoMode.Password)
        self.text.textEdited.connect(self._edited)
        self.controls.undoRequested.connect(lambda: self.load(*self._original))
        self.action.currentIndexChanged.connect(self._intent_changed)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.controls)

    def _edited(self, text):
        candidate = text if self.key in _SECRET_FIELDS else text.strip()
        self.action.setCurrentIndex(0 if candidate == self._initial_text else 1)

    def _intent_changed(self, state):
        self.text.setEnabled(state != 2)

    def load(self, value, source):
        self._original = (value, source)
        if self.key in _SECRET_FIELDS:
            current = "已设置（输入可更换）" if value else "输入 API Key"
            self._initial_text = ""
        elif isinstance(value, list):
            current = ", ".join(value)
            self._initial_text = current
        else:
            current = str(value) if value is not None else ""
            self._initial_text = current
        self.text.setText(self._initial_text)
        self.text.setPlaceholderText(current or "可选，使用服务默认地址")
        self.text.setToolTip(current)
        self.controls.load(source)
        self.text.setEnabled(True)

    def value(self):
        if self.action.currentIndex() == 2:
            return None
        text = self.text.text() if self.key in _SECRET_FIELDS else self.text.text().strip()
        if not text:
            raise ValueError(f"{_FIELDS[self.key]}：请输入值，或在更多操作中恢复默认")
        if self.key == "pageindex_threshold":
            try:
                value = int(text)
            except ValueError:
                raise ValueError("长 PDF 起始页数须为正整数") from None
            if value < 1:
                raise ValueError("长 PDF 起始页数须为正整数")
            return value
        if self.key == "entity_types":
            return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
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
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        self.status = QLabel("正在读取设置…")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.form = QWidget()
        form_layout = QVBoxLayout(self.form)
        form_layout.setContentsMargins(0, 8, 0, 8)
        form_layout.setSpacing(14)
        self.fields = {}
        connection = self._card("模型连接", "用于整理资料和知识库问答。", form_layout)
        for key in ("model", "openai_api_base", "api_key"):
            self._add_field(connection, key)
        content = self._card("内容偏好", "设置生成内容的语言。", form_layout)
        self._add_field(content, "language")
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setObjectName("disclosure")
        self.advanced_toggle.setText("高级内容选项")
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAccessibleName("高级内容选项")
        form_layout.addWidget(self.advanced_toggle)
        self.advanced_content = QWidget()
        advanced = QFormLayout(self.advanced_content)
        advanced.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        advanced.setSpacing(12)
        for key in ("pageindex_threshold", "entity_types"):
            self._add_field(advanced, key)
        form_layout.addWidget(self.advanced_content)
        self.advanced_content.hide()
        self.advanced_toggle.toggled.connect(self._show_advanced)
        form_layout.addStretch()
        from openkb.desktop.processing_settings import NavigationField, OcrField, ProcessingField

        tabs = QTabWidget()
        tabs.setObjectName("settingsCategories")
        tabs.setAccessibleName("设置分类")
        tabs.addTab(scroll_form(self.form), "模型与语言")
        self.fields["processing"] = ProcessingField()
        self.fields["parsing"] = OcrField(self.io, kb)
        self.fields["ocr_api_key"] = self.fields["parsing"].cloud_key
        tabs.addTab(scroll_form(self.fields["processing"]), "处理限制")
        tabs.addTab(scroll_form(self.fields["parsing"]), "文档识别")
        from openkb.desktop.image_settings import ImageField

        self.fields["image_understanding"] = ImageField(io, kb)
        self.fields["image_api_key"] = self.fields["image_understanding"].api_key
        tabs.addTab(scroll_form(self.fields["image_understanding"]), "图片理解")
        self.fields["navigation"] = NavigationField()
        tabs.addTab(scroll_form(self.fields["navigation"]), "原文导航")
        layout.addWidget(tabs, 1)
        self.editors = tabs
        footer = QFrame()
        footer.setObjectName("settingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 12, 0, 0)
        self.save_state = QLabel("所有更改已保存")
        self.save_state.setObjectName("muted")
        self.save_state.setWordWrap(True)
        footer_layout.addWidget(self.save_state, 1)
        self.discard_button = QPushButton("撤销修改")
        self.discard_button.clicked.connect(self.discard)
        footer_layout.addWidget(self.discard_button)
        self.repair_button = QPushButton("修复设置")
        self.repair_button.clicked.connect(self.repair)
        self.repair_button.hide()
        footer_layout.addWidget(self.repair_button)
        more = QToolButton()
        more.setObjectName("settingMenu")
        more.setText("⋯")
        more.setAccessibleName("设置维护")
        more.setToolTip("设置维护")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("检查并恢复设置…", self.repair)
        more.setMenu(menu)
        footer_layout.addWidget(more)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存更改")
        self.save_button.setObjectName("primaryAction")
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        footer_layout.addWidget(self.buttons)
        layout.addWidget(footer)
        self._view = None
        for field in self.fields.values():
            field.action.currentIndexChanged.connect(self._update_pending)
        self._update_pending()
        self.reload()

    def _card(self, title, description, layout):
        card = QFrame()
        card.setObjectName("settingsCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(20, 16, 20, 18)
        body.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        body.addWidget(heading)
        hint = QLabel(description)
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        body.addWidget(hint)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setSpacing(12)
        form.setContentsMargins(0, 8, 0, 0)
        body.addLayout(form)
        layout.addWidget(card)
        return form

    def _add_field(self, form, key):
        field = SettingField(key)
        self.fields[key] = field
        form.addRow(_FIELDS[key], field)

    def _show_advanced(self, checked):
        self.advanced_content.setVisible(checked)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _update_pending(self, *_):
        count = sum(field.action.currentIndex() != 0 for field in self.fields.values())
        idle = self._loaded and not self._saving and not self._loading
        self.save_button.setEnabled(idle and count > 0)
        self.discard_button.setVisible(count > 0)
        self.discard_button.setEnabled(idle)
        self.save_state.setText(
            "正在保存…" if self._saving else f"{count} 项未保存" if count else "所有更改已保存"
        )

    def discard(self):
        if self._view is not None and not self._saving and not self._loading:
            self.loaded(self._view, None)

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
        self._update_pending()
        self.form.setEnabled(False)
        self.editors.setEnabled(False)
        self.status.setText("正在读取设置…")
        self.io.submit(
            lambda: read_settings_view(self.kb),
            self.loaded,
            kb=self.kb,
            settings_read=True,
            obsolete=lambda: self._closed,
        )

    def loaded(self, view, error):
        if self._closed:
            return
        was_saving = self._saving
        self._saving = self._loading = False
        self.buttons.setEnabled(True)
        if error:
            self.form.setEnabled(self._loaded)
            self.editors.setEnabled(self._loaded)
            self.repair_button.show()
            self._update_pending()
            self.status.setText(f"设置操作失败（{type(error).__name__}），原文件及恢复资料会保留。")
            return
        self._loaded = True
        self._view = view
        self.repair_button.hide()
        self.form.setEnabled(True)
        self.editors.setEnabled(True)
        self.status.setText(
            f"仅应用到「{self.kb.name}」；未单独设置的项目沿用全局默认。"
            if self.kb
            else "应用到所有知识库；知识库中单独设置的项目优先。"
        )
        self.status.setToolTip(str(self.kb) if self.kb else "启动环境中的设置优先于此处配置。")
        for key, field in self.fields.items():
            value = getattr(view.values, _SECRET_FIELDS.get(key, key))
            field.load(value, view.sources[key])
        self._update_pending()
        if was_saving:
            self.save_state.setText("已保存 · 下次任务生效")

    def save(self):
        if not self._loaded or self._saving or self._loading or not self.form.isEnabled():
            return
        try:
            changes = {
                key: field.value()
                for key, field in self.fields.items()
                if field.action.currentIndex() != 0
            }
            credentials = {
                key: changes.pop(key)
                for key in (*_SECRET_FIELDS, "openai_api_base")
                if key in changes
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
        self._update_pending()
        self.form.setEnabled(False)
        self.editors.setEnabled(False)
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

        if self._saving or self._loading:
            return
        self._saving = True
        self._update_pending()
        self.form.setEnabled(False)
        self.editors.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setText("正在检查恢复资料及设置…")

        def operation():
            return repair_knowledge_base(self.kb) if self.kb else repair_global_settings()

        def repaired(result, error):
            if self._closed:
                return
            self._saving = False
            self.buttons.setEnabled(True)
            if error or not result.repaired:
                self.form.setEnabled(self._loaded)
                self.editors.setEnabled(self._loaded)
                self._update_pending()
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
                settings_read=True,
                obsolete=lambda: self._closed,
            )

        self.io.submit(
            operation, repaired, kb=self.kb, exclusive=True, global_settings=True, repair=True
        )

    def reject(self):
        if not self._saving:
            super().reject()

    def done(self, result):
        self._closed = True
        self.fields["parsing"].installer.retire()
        self.fields["image_understanding"].retire()
        super().done(result)

    def closeEvent(self, event):
        if self._saving:
            event.ignore()
        else:
            super().closeEvent(event)
